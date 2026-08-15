"""Appointment lifecycle transactions: booking, cancellation, rescheduling.

The caller selects *who* and *when* (lead, service, location, practitioner,
start instant). OdontoFlow — never the caller — owns the duration, the
capability check, the availability evaluation and the conflict verdict.

Two layers guard overlaps, and they are not interchangeable:

* the in-transaction preflight (this module) turns knowable problems into the
  stable ``AppError`` contract: inactive entities, missing capability, blocked
  intervals;
* the partial GiST exclusion constraint on ``appointments`` is the final
  concurrency authority. A racing insert fails at flush with SQLSTATE
  ``23P01``; that ``IntegrityError`` is deliberately **not** caught here so the
  transport layer (``app/errors.py``) can map it to the stable
  ``APPOINTMENT_CONFLICT`` 409 response.

Mutating an existing appointment (cancel, reschedule) adds a third guard: the
row is loaded ``FOR UPDATE`` as the first statement of the transaction, so two
operations on the *same* appointment are serialized by PostgreSQL and the
loser re-reads the committed state before deciding. State transition and audit
record always commit together.

Tenancy (PF1) adds a fourth layer that changes none of the above: every entity
is resolved inside the acting organization, the appointment carries its
``organization_id``, and the composite tenant foreign keys make a cross-tenant
appointment impossible to write. The overlap preflight below stays
**practitioner-global** on purpose (PF0 §9 S4) so it keeps mirroring the GiST
exclusion, which is itself deliberately tenant-agnostic (§9 S1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.catalog.models import Service
from app.commercial.models import Lead
from app.context import default_context
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.idempotency.models import CommandReceipt
from app.idempotency.service import IdempotencyClaim
from app.iam.permissions import (
    APPOINTMENTS_CANCEL,
    APPOINTMENTS_CREATE,
    APPOINTMENTS_RESCHEDULE,
)
from app.iam.service import require_permission
from app.organization.models import Location, Practitioner, PractitionerCapability
from app.organization.service import load_membership
from app.scheduling.availability import generate_slots
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock
from app.tenancy import scoped

UTC = timezone.utc

APPOINTMENT_ENTITY_TYPE = "appointment"
APPOINTMENT_CREATED_ACTION = "appointment.created"
APPOINTMENT_CANCELLED_ACTION = "appointment.cancelled"
APPOINTMENT_RESCHEDULED_ACTION = "appointment.rescheduled"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"


def _require_aware(start: datetime) -> datetime:
    if not isinstance(start, datetime):
        raise AppError(ErrorCode.INVALID_INPUT, "start must be a datetime.")
    if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
        raise AppError(ErrorCode.INVALID_INPUT, "start must be timezone-aware.")
    return start.astimezone(UTC)


def _load_active_scoped(
    session: Session, model, entity_id: int, organization_id: int, label: str
):
    """Load one active tenant-owned entity, or raise the stable contract error.

    The tenant filter lives in the query (§7.4): another organization's row is
    reported as ``NOT_FOUND``, exactly like an id that never existed.
    """
    entity = session.scalar(
        scoped(select(model).where(model.id == entity_id), model, organization_id)
    )
    if entity is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found.")
    if not entity.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, f"{label} is inactive.")
    return entity


def _load_active_member(
    session: Session, practitioner_id: int, organization_id: int
) -> Practitioner:
    """Load a practitioner this organization may schedule (PF0 PM3/T5).

    ``Practitioner`` is global, so it is *not* filtered by organization; the
    membership row is what makes it reachable from this tenant, and both
    activity flags must be true.
    """
    membership = load_membership(session, practitioner_id, organization_id)
    practitioner = session.get(Practitioner, practitioner_id)
    if practitioner is None:
        raise AppError(ErrorCode.NOT_FOUND, "Practitioner not found.")
    if not practitioner.is_active or not membership.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Practitioner is inactive.")
    return practitioner


def _require_capability(
    session: Session,
    practitioner_id: int,
    service_id: int,
    location_id: int,
    organization_id: int,
) -> None:
    capability = session.scalars(
        select(PractitionerCapability).where(
            PractitionerCapability.organization_id == organization_id,
            PractitionerCapability.practitioner_id == practitioner_id,
            PractitionerCapability.service_id == service_id,
            PractitionerCapability.location_id == location_id,
        )
    ).one_or_none()
    if capability is None or not capability.is_active:
        raise AppError(
            ErrorCode.CAPABILITY_MISSING,
            "The practitioner is not capable of this service at this location.",
        )


def _availability_inputs(
    session: Session,
    practitioner_id: int,
    location_id: int,
    start_utc: datetime,
    end_utc: datetime,
    organization_id: int,
    *,
    exclude_appointment_id: int | None = None,
):
    # Availability is published per organization *and* per branch (§9 S6), so a
    # practitioner shared by two tenants offers independent availability in each.
    rules = list(
        session.scalars(
            select(AvailabilityRule).where(
                AvailabilityRule.organization_id == organization_id,
                AvailabilityRule.practitioner_id == practitioner_id,
                AvailabilityRule.location_id == location_id,
            )
        )
    )
    blocks = list(
        session.scalars(
            select(ScheduleBlock).where(
                ScheduleBlock.organization_id == organization_id,
                ScheduleBlock.practitioner_id == practitioner_id,
                ScheduleBlock.location_id == location_id,
                ScheduleBlock.start_utc < end_utc,
                ScheduleBlock.end_utc > start_utc,
            )
        )
    )
    # Practitioner-wide: neither location- nor organization-scoped. The GiST
    # exclusion ignores both, so the preflight must too — otherwise a
    # cross-location or cross-organization double booking would reach the
    # database as a raw 23P01 instead of a clear SLOT_BLOCKED (§9 S4, F-16).
    conflicting = select(Appointment).where(
        Appointment.practitioner_id == practitioner_id,
        Appointment.state == CONFIRMED,
        Appointment.start_utc < end_utc,
        Appointment.end_utc > start_utc,
    )
    if exclude_appointment_id is not None:
        # Self-exclusion for rescheduling, applied at the query boundary: the
        # appointment being moved must not block its own new interval. The pure
        # availability engine stays unaware of the operation being performed.
        conflicting = conflicting.where(Appointment.id != exclude_appointment_id)
    appointments = list(session.scalars(conflicting))
    return rules, blocks, appointments


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    """Resolve the application-boundary context (PF0 §13 X1/X2).

    An explicit ``ctx`` is the authoritative contract and is used as-is. When a
    caller omits it (the pre-PF3 call style, still exercised by the fixtures),
    the trusted/default context applies: the seeded ``system`` principal in the
    acting organization (bootstrap by default), with a fresh ``request_id`` and
    ``correlation_id`` derived from it (X5). ``organization_id`` is honored as
    the acting tenant exactly like :func:`app.tenancy.resolve_organization_id`
    used to — PF3 substitutes ``ctx.organization_id`` for that body and keeps
    the bootstrap fallback for compatibility (tenancy.py docstring).
    """
    return ctx if ctx is not None else default_context(organization_id)


def _claim_receipt(
    session: Session, resolved: ExecutionContext, idempotency: IdempotencyClaim | None
) -> CommandReceipt | None:
    """Stage the idempotency claim as the transaction's first statement (§16.1).

    When ``idempotency`` is given (a keyed command), the claim row is added and
    flushed before anything else: a duplicate key surfaces here as ``23505`` on
    ``uq_command_receipts_org_operation_key`` — before permission evaluation,
    preflight reads, row locks or the GiST insert — so the command never holds
    a GiST or row lock while waiting on the receipt index, and the handler
    (``app/idempotency/service.py``) can resolve the collision. A duplicate
    claim therefore also can never outlive a rollback: the claim lives and
    dies with the mutation (I7/C3).
    """
    if idempotency is None:
        return None
    receipt = CommandReceipt(
        organization_id=resolved.organization_id,
        principal_id=resolved.principal_id,
        operation=idempotency.operation,
        idempotency_key=idempotency.key,
        request_fingerprint=idempotency.fingerprint,
        request_id=resolved.request_id,
        correlation_id=resolved.correlation_id,
    )
    session.add(receipt)
    session.flush()
    return receipt


def _settle_receipt(
    receipt: CommandReceipt | None,
    *,
    resource_type: str,
    resource_id: str,
    outcome_json: dict,
) -> None:
    """Fill the claim's outcome before commit (§16.1 step 6, I5/I13).

    The receipt row, the mutation and the audit row land in the same
    transaction or not at all, so a committed receipt always carries its
    logical outcome and a rolled-back command leaves no trace.
    """
    if receipt is None:
        return
    receipt.resource_type = resource_type
    receipt.resource_id = resource_id
    receipt.outcome_json = outcome_json


def _appointment_outcome(appointment: Appointment, *, status: str = "applied") -> dict:
    """The durable logical outcome of an appointment command (I5).

    Domain-level result only — never an HTTP status code or serialized
    response body. ``status`` is the command's own status (``applied``); the
    appointment's state is its own field.
    """
    return {
        "status": status,
        "resource_type": APPOINTMENT_ENTITY_TYPE,
        "resource_id": str(appointment.id),
        "state": appointment.state,
        "start_utc": appointment.start_utc.astimezone(UTC).isoformat(),
        "end_utc": appointment.end_utc.astimezone(UTC).isoformat(),
        "organization_id": appointment.organization_id,
        "lead_id": appointment.lead_id,
        "service_id": appointment.service_id,
        "practitioner_id": appointment.practitioner_id,
        "location_id": appointment.location_id,
    }


def book_appointment(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    lead_id: int,
    service_id: int,
    location_id: int,
    practitioner_id: int,
    start: datetime,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Appointment:
    """Confirm one appointment atomically and return it.

    ``ctx`` is the explicit application-boundary contract (PF0 §13): who is
    acting, in which organization, under which invocation. When omitted, the
    trusted/default context (seeded ``system`` principal + bootstrap org) keeps
    the pre-auth fixtures working. The authoritative permission check is the
    first statement of the transaction (E6/F-19); the tenant is taken from the
    context, never from a body field (X3).

    ``idempotency`` (PF4, §16.1) is the claim staged as the transaction's
    FIRST statement — before the permission check — when the caller supplied
    an ``Idempotency-Key``; the receipt row is settled with the logical
    outcome just before commit and commits atomically with the appointment
    and its audit row.

    ``start`` must be timezone-aware; it is normalized to UTC and the end is
    derived from the catalog duration. There is deliberately no ``end`` or
    ``duration_minutes`` parameter.

    The use case owns its transaction: it calls ``session.begin()`` before the
    first booking read, so it must be handed an idle ``Session``. Appointment
    and audit row commit together or not at all.

    Raises ``AppError`` (``PERMISSION_DENIED``, ``NOT_FOUND``,
    ``ENTITY_INACTIVE``, ``CAPABILITY_MISSING``, ``SLOT_BLOCKED``,
    ``INVALID_INPUT``) for preflight failures, and lets ``IntegrityError``
    (SQLSTATE ``23P01``) propagate when the exclusion constraint settles a race.
    """
    start_utc = _require_aware(start)
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = _claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, APPOINTMENTS_CREATE, location_id=location_id)
        if (
            session.scalar(scoped(select(Lead).where(Lead.id == lead_id), Lead, org_id))
            is None
        ):
            raise AppError(ErrorCode.NOT_FOUND, "Lead not found.")
        service = _load_active_scoped(session, Service, service_id, org_id, "Service")
        location = _load_active_scoped(session, Location, location_id, org_id, "Location")
        _load_active_member(session, practitioner_id, org_id)
        _require_capability(session, practitioner_id, service_id, location_id, org_id)

        duration_minutes = service.duration_minutes
        end_utc = start_utc + timedelta(minutes=duration_minutes)

        rules, blocks, appointments = _availability_inputs(
            session, practitioner_id, location_id, start_utc, end_utc, org_id
        )
        bookable = generate_slots(
            rules,
            blocks,
            appointments,
            duration_minutes,
            start_utc,
            end_utc,
            location.timezone,
        )
        if (start_utc, end_utc) not in bookable:
            raise AppError(
                ErrorCode.SLOT_BLOCKED,
                "The requested interval is not a bookable slot for this practitioner.",
            )

        appointment = Appointment(
            organization_id=org_id,
            lead_id=lead_id,
            service_id=service_id,
            practitioner_id=practitioner_id,
            location_id=location_id,
            start_utc=start_utc,
            end_utc=end_utc,
            state=CONFIRMED,
        )
        session.add(appointment)
        session.flush()  # assigns the id and lets the GiST rule on the interval

        record_event(
            session,
            ctx=resolved,
            entity_type=APPOINTMENT_ENTITY_TYPE,
            entity_id=str(appointment.id),
            action=APPOINTMENT_CREATED_ACTION,
            after_state={
                "id": appointment.id,
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
                "state": CONFIRMED,
            },
        )
        _settle_receipt(
            receipt,
            resource_type=APPOINTMENT_ENTITY_TYPE,
            resource_id=str(appointment.id),
            outcome_json=_appointment_outcome(appointment),
        )

    return appointment


def _lock_appointment(
    session: Session, appointment_id: int, organization_id: int
) -> Appointment:
    """Load one appointment ``FOR UPDATE`` as the transaction's first read.

    ``populate_existing`` forces the freshly locked row over anything the
    identity map may already hold, so the state check below always reflects
    what is committed *now* — the point of taking the lock. The tenant filter is
    part of the locking query, so another organization's appointment is
    ``NOT_FOUND`` and is never even locked (§7.4).
    """
    appointment = session.execute(
        scoped(
            select(Appointment).where(Appointment.id == appointment_id),
            Appointment,
            organization_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if appointment is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
    return appointment


def _require_confirmed(appointment: Appointment) -> None:
    """Reject any transition out of a non-confirmed appointment.

    Cancelling an already cancelled appointment is a stable conflict, not a
    silent no-op: the caller asked to change something that no longer exists in
    a changeable state, and idempotent cancellation would hide a genuine race
    between two actors.
    """
    if appointment.state != CONFIRMED:
        raise AppError(
            ErrorCode.ENTITY_INACTIVE,
            "The appointment is not confirmed and cannot be modified.",
        )


def _appointment_state(appointment: Appointment) -> dict:
    """Audit payload for one appointment version, in canonical UTC ISO-8601."""
    return {
        "id": appointment.id,
        "start_utc": appointment.start_utc.astimezone(UTC).isoformat(),
        "end_utc": appointment.end_utc.astimezone(UTC).isoformat(),
        "state": appointment.state,
    }


def cancel_appointment(
    session: Session,
    appointment_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Appointment:
    """Cancel one confirmed appointment atomically and return it.

    ``ctx`` is the explicit application-boundary contract; when omitted the
    trusted/default context applies (compatibility, ``app.context.default_context``).
    The authoritative permission check runs against the appointment's own
    location (F-4); the tenant-scoped lock is the existence check, so another
    organization's appointment is ``NOT_FOUND`` exactly like a non-existent one
    (E8, tenant isolation).

    ``idempotency`` (PF4) is the claim staged as the transaction's FIRST
    statement — before the row lock — when the caller supplied an
    ``Idempotency-Key``; the receipt is settled with the logical outcome just
    before commit (the cancelled state, interval preserved) and commits
    atomically with the mutation and its audit row.

    The interval is preserved: only ``state`` changes. Because the GiST
    exclusion is partial (``WHERE state = 'confirmed'``), the cancelled row
    stops consuming the practitioner's schedule the moment the transaction
    commits, and the interval becomes bookable again.

    Raises ``AppError``: ``PERMISSION_DENIED``, ``NOT_FOUND`` when the
    appointment does not exist, ``ENTITY_INACTIVE`` when it is not confirmed
    (double cancellation).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = _claim_receipt(session, resolved, idempotency)
        appointment = _lock_appointment(session, appointment_id, org_id)
        if ctx is not None:
            require_permission(
                session,
                resolved,
                APPOINTMENTS_CANCEL,
                location_id=appointment.location_id,
            )
        _require_confirmed(appointment)

        before_state = _appointment_state(appointment)
        appointment.state = CANCELLED
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=APPOINTMENT_ENTITY_TYPE,
            entity_id=str(appointment.id),
            action=APPOINTMENT_CANCELLED_ACTION,
            before_state=before_state,
            after_state=_appointment_state(appointment),
        )
        _settle_receipt(
            receipt,
            resource_type=APPOINTMENT_ENTITY_TYPE,
            resource_id=str(appointment.id),
            outcome_json=_appointment_outcome(appointment),
        )

    return appointment


def reschedule_appointment(
    session: Session,
    appointment_id: int,
    new_start: datetime,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Appointment:
    """Move one confirmed appointment to a new interval atomically.

    ``ctx`` is the explicit application-boundary contract; when omitted the
    trusted/default context applies (compatibility). The authoritative
    permission check runs against the appointment's own location (F-4).

    ``idempotency`` (PF4) is the claim staged as the transaction's FIRST
    statement — before the row lock — when the caller supplied an
    ``Idempotency-Key``; the receipt is settled with the logical outcome (the
    new interval, still confirmed) just before commit and commits atomically
    with the mutation and its audit row.

    The *same* row is updated: there is no new appointment, no temporary
    cancellation and therefore no visible ``old cancelled + new confirmed``
    transition. ``new_start`` must be timezone-aware; the end is always derived
    from the catalog duration, so there is deliberately no ``end`` or
    ``duration_minutes`` parameter.

    Every authority is revalidated inside the transaction exactly as booking
    does, with one difference: the appointment being moved is excluded from the
    confirmed set handed to the availability engine, so it cannot block itself.

    Raises ``AppError`` (``PERMISSION_DENIED``, ``NOT_FOUND``,
    ``ENTITY_INACTIVE``, ``CAPABILITY_MISSING``, ``SLOT_BLOCKED``,
    ``INVALID_INPUT``) for preflight failures, and lets ``IntegrityError``
    (SQLSTATE ``23P01``) propagate when the exclusion constraint settles a race.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = _claim_receipt(session, resolved, idempotency)
        appointment = _lock_appointment(session, appointment_id, org_id)
        if ctx is not None:
            require_permission(
                session,
                resolved,
                APPOINTMENTS_RESCHEDULE,
                location_id=appointment.location_id,
            )
        _require_confirmed(appointment)
        new_start_utc = _require_aware(new_start)

        # Authoritative reload: capability and active state may have changed
        # since the appointment was first confirmed. The appointment's own
        # organization is the authority here — it is guaranteed to equal
        # ``org_id`` by the tenant-scoped lock above.
        appointment_org_id = appointment.organization_id
        service = _load_active_scoped(
            session, Service, appointment.service_id, appointment_org_id, "Service"
        )
        location = _load_active_scoped(
            session, Location, appointment.location_id, appointment_org_id, "Location"
        )
        _load_active_member(session, appointment.practitioner_id, appointment_org_id)
        _require_capability(
            session,
            appointment.practitioner_id,
            appointment.service_id,
            appointment.location_id,
            appointment_org_id,
        )

        duration_minutes = service.duration_minutes
        new_end_utc = new_start_utc + timedelta(minutes=duration_minutes)

        rules, blocks, appointments = _availability_inputs(
            session,
            appointment.practitioner_id,
            appointment.location_id,
            new_start_utc,
            new_end_utc,
            appointment_org_id,
            exclude_appointment_id=appointment.id,
        )
        bookable = generate_slots(
            rules,
            blocks,
            appointments,
            duration_minutes,
            new_start_utc,
            new_end_utc,
            location.timezone,
        )
        if (new_start_utc, new_end_utc) not in bookable:
            raise AppError(
                ErrorCode.SLOT_BLOCKED,
                "The requested interval is not a bookable slot for this practitioner.",
            )

        before_state = _appointment_state(appointment)
        appointment.start_utc = new_start_utc
        appointment.end_utc = new_end_utc
        session.flush()  # lets the GiST rule on the moved interval

        record_event(
            session,
            ctx=resolved,
            entity_type=APPOINTMENT_ENTITY_TYPE,
            entity_id=str(appointment.id),
            action=APPOINTMENT_RESCHEDULED_ACTION,
            before_state=before_state,
            after_state=_appointment_state(appointment),
        )
        _settle_receipt(
            receipt,
            resource_type=APPOINTMENT_ENTITY_TYPE,
            resource_id=str(appointment.id),
            outcome_json=_appointment_outcome(appointment),
        )

    return appointment
