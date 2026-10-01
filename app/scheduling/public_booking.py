"""SELF — a patient books from the phone through the frontend BFF.

Spec: ``docs/superpowers/specs/2026-10-01-erp-self.md``. One command that
composes what already exists: the deterministic slot engine, the booking core
(``_book_appointment_core``) and the PF4 receipt. The BFF is an
``integration`` principal (profile ``patient-booking``); the patient is the
confirming party (L3), so the appointment is ``confirmed`` at once and no
proposal row is written.

Key contract: every patient goes through the same BFF principal and receipts
are keyed ``(org, 'public_booking.create', key)``, so the BFF mints a fresh
UUIDv4 per submit and reuses it only to retry that same submit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.commercial.models import Lead
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import APPOINTMENTS_CREATE
from app.iam.service import (
    PERMISSION_DENIED_HTTP_STATUS,
    PERMISSION_DENIED_MESSAGE,
    IamErrorCode,
    require_permission,
)
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.messaging.models import ContactIdentity
from app.organization.models import Location
from app.organization.service import list_eligible_practitioners
from app.scheduling.availability import generate_slots
from app.scheduling.models import Appointment
from app.scheduling.service import (
    APPOINTMENT_ENTITY_TYPE,
    _appointment_outcome,
    _availability_inputs,
    _book_appointment_core,
    _emit_appointment_event,
    _load_active_member,
    _load_active_scoped,
    _require_aware,
    _require_capability,
)

UTC = timezone.utc

OP_PUBLIC_BOOKING_CREATE = "public_booking.create"
#: Domain event type (``app/events/types.py`` is outside this card's surface).
APPOINTMENT_BOOKED_BY_PATIENT = "appointment.booked_by_patient"
PUBLIC_BOOKING_MAX_PER_PHONE_24H = 3
RATE_LIMIT_WINDOW_HOURS = 24
#: Only the BFF books as the patient: an agent holding ``appointments.create``
#: would otherwise skip the L3 confirmation of ``contact_appointments.book``.
PUBLIC_BOOKING_PRINCIPAL_TYPE = "integration"


class PublicBookingErrorCode(str, Enum):
    PUBLIC_BOOKING_RATE_LIMITED = "PUBLIC_BOOKING_RATE_LIMITED"


def _rate_limited() -> AppError:
    return AppError(
        PublicBookingErrorCode.PUBLIC_BOOKING_RATE_LIMITED,
        "Too many bookings for this phone. Please call the clinic.",
        details={
            "limit": PUBLIC_BOOKING_MAX_PER_PHONE_24H,
            "window_hours": RATE_LIMIT_WINDOW_HOURS,
        },
        http_status=429,
    )


def _deny() -> AppError:
    return AppError(
        IamErrorCode.PERMISSION_DENIED,
        PERMISSION_DENIED_MESSAGE,
        details={},
        http_status=PERMISSION_DENIED_HTTP_STATUS,
    )


def reference_for(appointment_id: int) -> str:
    return f"OF-{appointment_id}"


def _resolve_lead_id(session: Session, organization_id: int, phone: str) -> int | None:
    """The lead this phone already belongs to, if any (exact E.164 match)."""
    lead_id = session.scalar(
        select(ContactIdentity.lead_id)
        .where(
            ContactIdentity.organization_id == organization_id,
            ContactIdentity.normalized_phone_e164 == phone,
            ContactIdentity.lead_id.is_not(None),
        )
        .order_by(ContactIdentity.created_at.desc(), ContactIdentity.id.desc())
        .limit(1)
    )
    if lead_id is not None:
        return lead_id
    return session.scalar(
        select(Lead.id)
        .where(Lead.organization_id == organization_id, Lead.contact_phone == phone)
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(1)
    )


def _recent_bookings(
    session: Session, organization_id: int, phone: str, lead_id: int | None
) -> int:
    match = Lead.contact_phone == phone
    if lead_id is not None:
        match = or_(Appointment.lead_id == lead_id, match)
    return session.scalar(
        select(func.count())
        .select_from(Appointment)
        .join(
            Lead,
            (Lead.organization_id == Appointment.organization_id)
            & (Lead.id == Appointment.lead_id),
        )
        .where(
            Appointment.organization_id == organization_id,
            Appointment.created_at
            > func.now() - timedelta(hours=RATE_LIMIT_WINDOW_HOURS),
            match,
        )
    )


def _candidates(
    session: Session,
    organization_id: int,
    service_id: int,
    location_id: int,
    practitioner_id: int | None,
) -> list[int]:
    """Who may take the slot; capability is settled before the slot is classified."""
    if practitioner_id is not None:
        _load_active_member(session, practitioner_id, organization_id)
        _require_capability(session, practitioner_id, service_id, location_id, organization_id)
        return [practitioner_id]
    eligible = list_eligible_practitioners(
        session, service_id, location_id, organization_id=organization_id
    )
    if not eligible:
        raise AppError(
            ErrorCode.CAPABILITY_MISSING,
            "No practitioner offers this service at this location.",
        )
    return sorted(p.id for p in eligible)


def _pick_practitioner(
    session: Session,
    organization_id: int,
    candidates: list[int],
    location: Location,
    start_utc: datetime,
    end_utc: datetime,
    duration_minutes: int,
) -> int:
    """422 when ``start`` is not a slot of anyone's rules; 409 when all are taken."""
    on_grid: list[tuple[int, list]] = []
    for practitioner_id in candidates:
        rules, blocks, appointments = _availability_inputs(
            session, practitioner_id, location.id, start_utc, end_utc, organization_id
        )
        free_of_appointments = generate_slots(
            rules, blocks, [], duration_minutes, start_utc, end_utc, location.timezone
        )
        if (start_utc, end_utc) in free_of_appointments:
            on_grid.append((practitioner_id, (rules, blocks, appointments)))
    if not on_grid:
        raise AppError(
            ErrorCode.INVALID_INPUT, "The requested start is outside availability."
        )
    for practitioner_id, (rules, blocks, appointments) in on_grid:
        bookable = generate_slots(
            rules, blocks, appointments, duration_minutes, start_utc, end_utc, location.timezone
        )
        if (start_utc, end_utc) in bookable:
            return practitioner_id
    raise AppError(
        ErrorCode.SLOT_BLOCKED, "The requested slot is no longer available."
    )


def book_public(
    session: Session,
    *,
    ctx: ExecutionContext,
    service_id: int,
    location_id: int,
    practitioner_id: int | None,
    start: datetime,
    full_name: str,
    phone: str,
    idempotency: IdempotencyClaim | None = None,
) -> dict:
    """Book one confirmed appointment for an anonymous patient; return its outcome.

    One transaction: claim → permission → past check → per-phone lock →
    lead resolution → rate limit → service/location → capability → slot
    classification → lead insert → booking core → domain event → receipt.
    """
    if ctx.principal_type != PUBLIC_BOOKING_PRINCIPAL_TYPE:
        raise _deny()
    start_utc = _require_aware(start)
    org_id = ctx.organization_id

    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, APPOINTMENTS_CREATE, location_id=location_id)
        if start_utc <= datetime.now(UTC):
            raise AppError(ErrorCode.INVALID_INPUT, "The requested start is in the past.")
        # Serializes one phone's bookings so the rate-limit count cannot race.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, :org))"),
            {"key": f"public_booking:{phone}", "org": org_id},
        )
        lead_id = _resolve_lead_id(session, org_id, phone)
        if _recent_bookings(session, org_id, phone, lead_id) >= PUBLIC_BOOKING_MAX_PER_PHONE_24H:
            raise _rate_limited()

        service = _load_active_scoped(session, Service, service_id, org_id, "Service")
        location = _load_active_scoped(session, Location, location_id, org_id, "Location")
        candidates = _candidates(session, org_id, service_id, location_id, practitioner_id)
        end_utc = start_utc + timedelta(minutes=service.duration_minutes)
        chosen = _pick_practitioner(
            session, org_id, candidates, location, start_utc, end_utc, service.duration_minutes
        )

        if lead_id is None:
            lead = Lead(
                organization_id=org_id,
                full_name=full_name,
                contact_phone=phone,
                acquisition_source="direct",
                service_need_id=service_id,
            )
            session.add(lead)
            session.flush()
            lead_id = lead.id

        appointment = _book_appointment_core(
            session,
            resolved=ctx,
            lead_id=lead_id,
            service_id=service_id,
            location_id=location_id,
            practitioner_id=chosen,
            start_utc=start_utc,
        )
        _emit_appointment_event(session, ctx, appointment, APPOINTMENT_BOOKED_BY_PATIENT)
        outcome = {**_appointment_outcome(appointment), "reference": reference_for(appointment.id)}
        settle_receipt(
            receipt,
            resource_type=APPOINTMENT_ENTITY_TYPE,
            resource_id=str(appointment.id),
            outcome_json=outcome,
        )
    return outcome
