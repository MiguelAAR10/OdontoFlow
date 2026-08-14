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
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.catalog.models import Service
from app.commercial.models import Lead
from app.errors import AppError, ErrorCode
from app.organization.models import Location, Practitioner, PractitionerCapability
from app.scheduling.availability import generate_slots
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock

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


def _load_active(session: Session, model, entity_id: int, label: str):
    entity = session.get(model, entity_id)
    if entity is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found.")
    if not entity.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, f"{label} is inactive.")
    return entity


def _require_capability(
    session: Session, practitioner_id: int, service_id: int, location_id: int
) -> None:
    capability = session.scalars(
        select(PractitionerCapability).where(
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
    *,
    exclude_appointment_id: int | None = None,
):
    rules = list(
        session.scalars(
            select(AvailabilityRule).where(
                AvailabilityRule.practitioner_id == practitioner_id,
                AvailabilityRule.location_id == location_id,
            )
        )
    )
    blocks = list(
        session.scalars(
            select(ScheduleBlock).where(
                ScheduleBlock.practitioner_id == practitioner_id,
                ScheduleBlock.location_id == location_id,
                ScheduleBlock.start_utc < end_utc,
                ScheduleBlock.end_utc > start_utc,
            )
        )
    )
    # Practitioner-wide, not location-scoped: the GiST exclusion ignores the
    # location, so the preflight must too or a cross-location double booking
    # would reach the database as a raw conflict instead of a clear error.
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


def book_appointment(
    session: Session,
    *,
    lead_id: int,
    service_id: int,
    location_id: int,
    practitioner_id: int,
    start: datetime,
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> Appointment:
    """Confirm one appointment atomically and return it.

    ``start`` must be timezone-aware; it is normalized to UTC and the end is
    derived from the catalog duration. There is deliberately no ``end`` or
    ``duration_minutes`` parameter.

    The use case owns its transaction: it calls ``session.begin()`` before the
    first booking read, so it must be handed an idle ``Session``. Appointment
    and audit row commit together or not at all.

    Raises ``AppError`` (``NOT_FOUND``, ``ENTITY_INACTIVE``,
    ``CAPABILITY_MISSING``, ``SLOT_BLOCKED``, ``INVALID_INPUT``) for preflight
    failures, and lets ``IntegrityError`` (SQLSTATE ``23P01``) propagate when
    the exclusion constraint settles a race.
    """
    start_utc = _require_aware(start)

    with session.begin():
        if session.get(Lead, lead_id) is None:
            raise AppError(ErrorCode.NOT_FOUND, "Lead not found.")
        service = _load_active(session, Service, service_id, "Service")
        location = _load_active(session, Location, location_id, "Location")
        _load_active(session, Practitioner, practitioner_id, "Practitioner")
        _require_capability(session, practitioner_id, service_id, location_id)

        duration_minutes = service.duration_minutes
        end_utc = start_utc + timedelta(minutes=duration_minutes)

        rules, blocks, appointments = _availability_inputs(
            session, practitioner_id, location_id, start_utc, end_utc
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
            entity_type=APPOINTMENT_ENTITY_TYPE,
            entity_id=str(appointment.id),
            action=APPOINTMENT_CREATED_ACTION,
            after_state={
                "id": appointment.id,
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
                "state": CONFIRMED,
            },
            actor_id=actor_id,
            actor_type=actor_type,
            correlation_id=correlation_id,
        )

    return appointment


def _lock_appointment(session: Session, appointment_id: int) -> Appointment:
    """Load one appointment ``FOR UPDATE`` as the transaction's first read.

    ``populate_existing`` forces the freshly locked row over anything the
    identity map may already hold, so the state check below always reflects
    what is committed *now* — the point of taking the lock.
    """
    appointment = session.execute(
        select(Appointment)
        .where(Appointment.id == appointment_id)
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
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> Appointment:
    """Cancel one confirmed appointment atomically and return it.

    The interval is preserved: only ``state`` changes. Because the GiST
    exclusion is partial (``WHERE state = 'confirmed'``), the cancelled row
    stops consuming the practitioner's schedule the moment the transaction
    commits, and the interval becomes bookable again.

    Raises ``AppError``: ``NOT_FOUND`` when the appointment does not exist,
    ``ENTITY_INACTIVE`` when it is not confirmed (double cancellation).
    """
    with session.begin():
        appointment = _lock_appointment(session, appointment_id)
        _require_confirmed(appointment)

        before_state = _appointment_state(appointment)
        appointment.state = CANCELLED
        session.flush()

        record_event(
            session,
            entity_type=APPOINTMENT_ENTITY_TYPE,
            entity_id=str(appointment.id),
            action=APPOINTMENT_CANCELLED_ACTION,
            before_state=before_state,
            after_state=_appointment_state(appointment),
            actor_id=actor_id,
            actor_type=actor_type,
            correlation_id=correlation_id,
        )

    return appointment


def reschedule_appointment(
    session: Session,
    appointment_id: int,
    new_start: datetime,
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> Appointment:
    """Move one confirmed appointment to a new interval atomically.

    The *same* row is updated: there is no new appointment, no temporary
    cancellation and therefore no visible ``old cancelled + new confirmed``
    transition. ``new_start`` must be timezone-aware; the end is always derived
    from the catalog duration, so there is deliberately no ``end`` or
    ``duration_minutes`` parameter.

    Every authority is revalidated inside the transaction exactly as booking
    does, with one difference: the appointment being moved is excluded from the
    confirmed set handed to the availability engine, so it cannot block itself.

    Raises ``AppError`` (``NOT_FOUND``, ``ENTITY_INACTIVE``,
    ``CAPABILITY_MISSING``, ``SLOT_BLOCKED``, ``INVALID_INPUT``) for preflight
    failures, and lets ``IntegrityError`` (SQLSTATE ``23P01``) propagate when
    the exclusion constraint settles a race.
    """
    with session.begin():
        appointment = _lock_appointment(session, appointment_id)
        _require_confirmed(appointment)
        new_start_utc = _require_aware(new_start)

        # Authoritative reload: capability and active state may have changed
        # since the appointment was first confirmed.
        service = _load_active(session, Service, appointment.service_id, "Service")
        location = _load_active(session, Location, appointment.location_id, "Location")
        _load_active(session, Practitioner, appointment.practitioner_id, "Practitioner")
        _require_capability(
            session,
            appointment.practitioner_id,
            appointment.service_id,
            appointment.location_id,
        )

        duration_minutes = service.duration_minutes
        new_end_utc = new_start_utc + timedelta(minutes=duration_minutes)

        rules, blocks, appointments = _availability_inputs(
            session,
            appointment.practitioner_id,
            appointment.location_id,
            new_start_utc,
            new_end_utc,
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
            entity_type=APPOINTMENT_ENTITY_TYPE,
            entity_id=str(appointment.id),
            action=APPOINTMENT_RESCHEDULED_ACTION,
            before_state=before_state,
            after_state=_appointment_state(appointment),
            actor_id=actor_id,
            actor_type=actor_type,
            correlation_id=correlation_id,
        )

    return appointment
