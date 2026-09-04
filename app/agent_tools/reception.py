"""Deterministic, contact-bound receptionist mutations and public context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_tools.schemas import (
    AgentToolCall,
    ConfirmCancellationArguments,
    ConfirmRescheduleArguments,
    EmptyArguments,
    HumanHandoffArguments,
    ProposeCancellationArguments,
    ProposeRescheduleArguments,
    ReceptionContextArguments,
    RegisterContactProfileArguments,
)
from app.agent_tools.guards import require_automation_active
from app.audit.service import record_event
from app.catalog.models import Promotion, Service
from app.clinical.models import Patient
from app.commercial.models import Lead
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    CONTACT_APPOINTMENTS_READ,
    CONTACT_APPOINTMENTS_CANCEL,
    CONTACT_APPOINTMENTS_RESCHEDULE,
    CONTACT_PROFILES_MANAGE,
    CONVERSATIONS_MANAGE,
    CONVERSATIONS_RESUME,
    LOCATIONS_READ,
    SERVICES_READ,
)
from app.iam.service import require_permission
from app.idempotency.service import (
    IdempotencyClaim,
    claim_receipt,
    run_idempotent_command,
    settle_receipt,
)
from app.messaging.models import (
    ContactIdentity,
    Conversation,
    Message,
    ReceptionHandoff,
)
from app.organization.models import Location, Organization
from app.scheduling.availability import generate_slots
from app.scheduling.models import (
    Appointment,
    AppointmentCancellationProposal,
    AppointmentProposal,
    AppointmentRescheduleProposal,
)
from app.scheduling.service import (
    _availability_inputs,
    _load_active_member,
    _load_active_scoped,
    _require_capability,
    _require_confirmed,
)

PROPOSAL_TTL = timedelta(minutes=15)


def _money(value: Decimal | None) -> str | None:
    return format(value, ".2f") if value is not None else None


def reception_context(
    session: Session,
    *,
    arguments: ReceptionContextArguments,
    conversation: Conversation,
    contact: ContactIdentity,
    ctx: ExecutionContext,
) -> dict:
    require_permission(session, ctx, SERVICES_READ)
    require_permission(session, ctx, LOCATIONS_READ)
    require_permission(session, ctx, CONTACT_APPOINTMENTS_READ)
    organization = session.get(Organization, ctx.organization_id)
    if organization is None:
        raise AppError(ErrorCode.NOT_FOUND, "Organization not found.")
    services = list(
        session.scalars(
            select(Service)
            .where(
                Service.organization_id == ctx.organization_id,
                Service.is_active.is_(True),
            )
            .order_by(Service.name)
        )
    )
    locations = list(
        session.scalars(
            select(Location)
            .where(
                Location.organization_id == ctx.organization_id,
                Location.is_active.is_(True),
            )
            .order_by(Location.name)
        )
    )
    promotions = list(
        session.scalars(
            select(Promotion)
            .where(
                Promotion.organization_id == ctx.organization_id,
                Promotion.is_active.is_(True),
                Promotion.valid_from <= arguments.as_of,
                Promotion.valid_until >= arguments.as_of,
            )
            .order_by(Promotion.priority.desc(), Promotion.code)
        )
    )
    now = datetime.now(UTC)
    recent_messages = list(
        session.scalars(
            select(Message)
            .where(
                Message.organization_id == ctx.organization_id,
                Message.conversation_id == conversation.id,
                Message.content_expires_at > now,
                Message.content_redacted_at.is_(None),
            )
            .order_by(Message.occurred_at.desc(), Message.id.desc())
            .limit(8)
        )
    )
    recent_messages.reverse()
    latest_inbound_message_id = next(
        (row.id for row in reversed(recent_messages) if row.direction == "inbound"),
        None,
    )
    appointments = []
    if contact.lead_id is not None:
        appointments = list(
            session.scalars(
                select(Appointment)
                .where(
                    Appointment.organization_id == ctx.organization_id,
                    Appointment.lead_id == contact.lead_id,
                    Appointment.state == "confirmed",
                    Appointment.start_utc >= now,
                )
                .order_by(Appointment.start_utc)
                .limit(10)
            )
        )
    pending_candidates: list[tuple[datetime, dict]] = []
    booking = session.scalar(
        select(AppointmentProposal)
        .where(
            AppointmentProposal.organization_id == ctx.organization_id,
            AppointmentProposal.conversation_id == conversation.id,
            AppointmentProposal.contact_identity_id == contact.id,
            AppointmentProposal.status == "pending",
            AppointmentProposal.expires_at > now,
        )
        .order_by(AppointmentProposal.created_at.desc(), AppointmentProposal.id.desc())
        .limit(1)
    )
    if booking is not None:
        pending_candidates.append(
            (
                booking.created_at,
                {
                    "action_type": "BOOK",
                    "proposal_id": booking.id,
                    "confirmation_token": str(booking.confirmation_token),
                    "service_id": booking.service_id,
                    "location_id": booking.location_id,
                    "practitioner_id": booking.practitioner_id,
                    "start": booking.start_utc.astimezone(UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "end": booking.end_utc.astimezone(UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "expires_at": booking.expires_at.astimezone(UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                },
            )
        )
    cancellation = session.scalar(
        select(AppointmentCancellationProposal)
        .where(
            AppointmentCancellationProposal.organization_id == ctx.organization_id,
            AppointmentCancellationProposal.conversation_id == conversation.id,
            AppointmentCancellationProposal.contact_identity_id == contact.id,
            AppointmentCancellationProposal.status == "pending",
            AppointmentCancellationProposal.expires_at > now,
        )
        .order_by(
            AppointmentCancellationProposal.created_at.desc(),
            AppointmentCancellationProposal.id.desc(),
        )
        .limit(1)
    )
    if cancellation is not None:
        pending_candidates.append(
            (
                cancellation.created_at,
                {
                    "action_type": "CANCEL",
                    "proposal_id": cancellation.id,
                    "confirmation_token": str(cancellation.confirmation_token),
                    "appointment_id": cancellation.appointment_id,
                    "created_from_message_id": cancellation.source_message_id,
                    "expires_at": cancellation.expires_at.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        )
    reschedule = session.scalar(
        select(AppointmentRescheduleProposal)
        .where(
            AppointmentRescheduleProposal.organization_id == ctx.organization_id,
            AppointmentRescheduleProposal.conversation_id == conversation.id,
            AppointmentRescheduleProposal.contact_identity_id == contact.id,
            AppointmentRescheduleProposal.status == "pending",
            AppointmentRescheduleProposal.expires_at > now,
        )
        .order_by(
            AppointmentRescheduleProposal.created_at.desc(),
            AppointmentRescheduleProposal.id.desc(),
        )
        .limit(1)
    )
    if reschedule is not None:
        pending_candidates.append(
            (
                reschedule.created_at,
                {
                    "action_type": "RESCHEDULE",
                    "proposal_id": reschedule.id,
                    "confirmation_token": str(reschedule.confirmation_token),
                    "appointment_id": reschedule.appointment_id,
                    "old_start": reschedule.old_start_utc.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "new_start": reschedule.new_start_utc.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "expires_at": reschedule.expires_at.astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        )
    pending_action = (
        max(pending_candidates, key=lambda item: item[0])[1]
        if pending_candidates
        else None
    )
    return {
        "organization": {"name": organization.name},
        "services": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.public_description,
                "duration_minutes": row.duration_minutes,
                "base_price": _money(row.base_price),
                "currency": row.currency,
                "booking_mode": row.booking_mode,
            }
            for row in services
        ],
        "locations": [
            {
                "id": row.id,
                "name": row.name,
                "timezone": row.timezone,
                "address": row.address,
                "public_phone": row.public_phone,
                "opening_hours": row.opening_hours,
            }
            for row in locations
        ],
        "promotions": [
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "description": row.description,
                "service_id": row.service_id,
                "promotional_price": _money(row.promotional_price),
                "discount_percent": _money(row.discount_percent),
                "currency": row.currency,
                "new_patients_only": row.new_patients_only,
                "valid_from": row.valid_from.isoformat(),
                "valid_until": row.valid_until.isoformat(),
            }
            for row in promotions
        ],
        "conversation": {
            "id": conversation.id,
            "status": conversation.status,
            "latest_inbound_message_id": latest_inbound_message_id,
        },
        "contact_profile": contact_profile(session, contact=contact, ctx=ctx),
        "recent_messages": [
            {
                "id": row.id,
                "direction": row.direction,
                "text": row.body_text,
                "occurred_at": row.occurred_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for row in recent_messages
        ],
        "appointments": [
            {
                "id": row.id,
                "service_id": row.service_id,
                "location_id": row.location_id,
                "practitioner_id": row.practitioner_id,
                "start": row.start_utc.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
                "end": row.end_utc.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
                "state": row.state,
            }
            for row in appointments
        ],
        "pending_action": pending_action,
        "safety": {
            "no_diagnosis": True,
            "no_prescriptions": True,
            "human_handoff_for_urgency": True,
        },
    }


def _load_conversation_contact(
    session: Session,
    *,
    conversation_id: int,
    ctx: ExecutionContext,
    for_update: bool = False,
    allow_human_handoff: bool = False,
) -> tuple[Conversation, ContactIdentity]:
    statement = select(Conversation).where(
        Conversation.organization_id == ctx.organization_id,
        Conversation.id == conversation_id,
    )
    if for_update:
        statement = statement.with_for_update()
    conversation = session.scalar(statement)
    if conversation is None or conversation.status == "closed":
        raise AppError(ErrorCode.NOT_FOUND, "Conversation not found.")
    if not allow_human_handoff:
        require_automation_active(conversation)
    contact = session.scalar(
        select(ContactIdentity).where(
            ContactIdentity.organization_id == ctx.organization_id,
            ContactIdentity.id == conversation.contact_identity_id,
        )
    )
    if contact is None:
        raise AppError(ErrorCode.NOT_FOUND, "Conversation not found.")
    return conversation, contact


def _profile_outcome(contact: ContactIdentity, patient: Patient | None) -> dict:
    return {
        "status": "registered" if patient is not None else "unregistered",
        "resource_type": "contact_profile",
        "resource_id": str(contact.id),
        "contact_identity_id": contact.id,
        "patient_id": patient.id if patient is not None else None,
        "lead_id": contact.lead_id,
        "full_name": patient.full_name if patient is not None else None,
        "birth_date": patient.birth_date.isoformat()
        if patient is not None and patient.birth_date is not None
        else None,
    }


def contact_profile(session: Session, *, contact: ContactIdentity, ctx: ExecutionContext) -> dict:
    patient = None
    if contact.patient_id is not None:
        patient = session.scalar(
            select(Patient).where(
                Patient.organization_id == ctx.organization_id,
                Patient.id == contact.patient_id,
            )
        )
        if patient is None:
            raise AppError(ErrorCode.NOT_FOUND, "Patient profile not found.")
    return _profile_outcome(contact, patient)


def ensure_contact_profile(
    session: Session,
    *,
    contact: ContactIdentity,
    full_name: str,
    ctx: ExecutionContext,
    dni: str | None = None,
    birth_date=None,
) -> tuple[Patient, Lead]:
    """Create or bind one patient/lead without accepting caller-owned ids."""
    normalized_name = " ".join(full_name.split())
    if len(normalized_name) < 2:
        raise AppError(ErrorCode.INVALID_INPUT, "A patient name is required.")

    patient = None
    if contact.patient_id is not None:
        patient = session.scalar(
            select(Patient).where(
                Patient.organization_id == ctx.organization_id,
                Patient.id == contact.patient_id,
            )
        )
        if patient is None:
            raise AppError(ErrorCode.NOT_FOUND, "Patient profile not found.")
    else:
        by_phone = list(
            session.scalars(
                select(Patient).where(
                    Patient.organization_id == ctx.organization_id,
                    Patient.phone == contact.normalized_phone_e164,
                )
            )
        )
        if len(by_phone) > 1:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "More than one patient uses this phone; human verification is required.",
            )
        if by_phone:
            patient = by_phone[0]
        elif dni is not None:
            patient = session.scalar(
                select(Patient).where(
                    Patient.organization_id == ctx.organization_id,
                    Patient.dni == dni,
                )
            )
            if patient is not None and patient.phone not in (
                None,
                contact.normalized_phone_e164,
            ):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The patient identity requires human verification.",
                )
        if patient is None:
            patient = Patient(
                organization_id=ctx.organization_id,
                full_name=normalized_name,
                dni=dni,
                phone=contact.normalized_phone_e164,
                birth_date=birth_date,
            )
            session.add(patient)
            session.flush()
        contact.patient_id = patient.id

    if dni is not None and patient.dni not in (None, dni):
        raise AppError(ErrorCode.INVALID_INPUT, "The patient identity does not match.")
    if birth_date is not None and patient.birth_date not in (None, birth_date):
        raise AppError(ErrorCode.INVALID_INPUT, "The patient identity does not match.")
    patient.full_name = normalized_name
    patient.phone = contact.normalized_phone_e164
    if patient.dni is None:
        patient.dni = dni
    if patient.birth_date is None:
        patient.birth_date = birth_date

    if contact.lead_id is None:
        lead = Lead(
            organization_id=ctx.organization_id,
            full_name=normalized_name,
            contact_phone=contact.normalized_phone_e164,
            contact_email=None,
            acquisition_source="direct",
        )
        session.add(lead)
        session.flush()
        contact.lead_id = lead.id
    else:
        lead = session.scalar(
            select(Lead).where(
                Lead.organization_id == ctx.organization_id,
                Lead.id == contact.lead_id,
            )
        )
        if lead is None:
            raise AppError(ErrorCode.NOT_FOUND, "Contact lead not found.")
        lead.full_name = normalized_name
        lead.contact_phone = contact.normalized_phone_e164
    return patient, lead


def register_contact_profile(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: RegisterContactProfileArguments,
    idempotency: IdempotencyClaim | None = None,
) -> dict:
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONTACT_PROFILES_MANAGE)
        _conversation, contact = _load_conversation_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        patient, _lead = ensure_contact_profile(
            session,
            contact=contact,
            full_name=arguments.full_name,
            dni=arguments.dni,
            birth_date=arguments.birth_date,
            ctx=ctx,
        )
        outcome = _profile_outcome(contact, patient)
        record_event(
            session,
            ctx=ctx,
            entity_type="contact_profile",
            entity_id=str(contact.id),
            action="contact_profile.registered",
            after_state={
                "contact_identity_id": contact.id,
                "patient_id": patient.id,
                "lead_id": contact.lead_id,
            },
        )
        settle_receipt(
            receipt,
            resource_type="contact_profile",
            resource_id=str(contact.id),
            outcome_json=outcome,
        )
    return outcome


def run_register_contact_profile_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: RegisterContactProfileArguments,
    ctx: ExecutionContext,
) -> dict:
    params = {"conversation_id": call.conversation_id, "arguments": arguments}
    outcome = run_idempotent_command(
        session,
        operation=register_contact_profile,
        operation_name="contact_profiles.register",
        key=str(call.idempotency_key),
        ctx=ctx,
        params={"conversation_id": call.conversation_id, **arguments.model_dump(mode="json")},
        **params,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    return {"profile": value, "replayed": outcome.replayed}


def _appointment_outcome(appointment: Appointment) -> dict:
    return {
        "status": "applied",
        "resource_type": "appointment",
        "resource_id": str(appointment.id),
        "patient_id": appointment.patient_id,
        "service_id": appointment.service_id,
        "practitioner_id": appointment.practitioner_id,
        "location_id": appointment.location_id,
        "start_utc": appointment.start_utc.astimezone(UTC).isoformat(),
        "end_utc": appointment.end_utc.astimezone(UTC).isoformat(),
        "state": appointment.state,
    }


def _appointment_dto(value: Appointment | dict) -> dict:
    outcome = _appointment_outcome(value) if isinstance(value, Appointment) else value
    return {
        "id": int(outcome["resource_id"]),
        "patient_id": outcome.get("patient_id"),
        "service_id": outcome["service_id"],
        "practitioner_id": outcome["practitioner_id"],
        "location_id": outcome["location_id"],
        "start": datetime.fromisoformat(outcome["start_utc"])
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "end": datetime.fromisoformat(outcome["end_utc"])
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "state": outcome["state"],
    }


def _own_appointment(
    session: Session,
    *,
    appointment_id: int,
    contact: ContactIdentity,
    ctx: ExecutionContext,
    for_update: bool = False,
) -> Appointment:
    if contact.lead_id is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
    statement = select(Appointment).where(
        Appointment.organization_id == ctx.organization_id,
        Appointment.id == appointment_id,
        Appointment.lead_id == contact.lead_id,
    )
    if for_update:
        statement = statement.with_for_update()
    appointment = session.scalar(statement)
    if appointment is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
    return appointment


def _cancellation_outcome(proposal: AppointmentCancellationProposal) -> dict:
    return {
        "status": proposal.status,
        "resource_type": "appointment_cancellation_proposal",
        "resource_id": str(proposal.id),
        "proposal_id": proposal.id,
        "appointment_id": proposal.appointment_id,
        "confirmation_token": str(proposal.confirmation_token),
        "source_message_id": proposal.source_message_id,
        "expires_at": proposal.expires_at.astimezone(UTC).isoformat(),
    }


def _cancellation_dto(value: AppointmentCancellationProposal | dict) -> dict:
    outcome = (
        _cancellation_outcome(value)
        if isinstance(value, AppointmentCancellationProposal)
        else value
    )
    return {
        "id": int(outcome.get("proposal_id", outcome["resource_id"])),
        "appointment_id": int(outcome["appointment_id"]),
        "confirmation_token": outcome["confirmation_token"],
        "source_message_id": int(outcome["source_message_id"]),
        "expires_at": outcome["expires_at"],
        "status": outcome["status"],
    }


def _require_inbound_message(
    session: Session,
    *,
    conversation_id: int,
    message_id: int,
    ctx: ExecutionContext,
) -> Message:
    message = session.scalar(
        select(Message).where(
            Message.organization_id == ctx.organization_id,
            Message.conversation_id == conversation_id,
            Message.id == message_id,
            Message.direction == "inbound",
        )
    )
    if message is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Confirmation must reference an inbound message from this conversation.",
        )
    return message


def create_cancellation_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: ProposeCancellationArguments,
    idempotency: IdempotencyClaim | None = None,
) -> AppointmentCancellationProposal:
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONTACT_APPOINTMENTS_CANCEL)
        conversation, contact = _load_conversation_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        appointment = _own_appointment(
            session,
            appointment_id=arguments.appointment_id,
            contact=contact,
            ctx=ctx,
            for_update=True,
        )
        _require_confirmed(appointment)
        _require_inbound_message(
            session,
            conversation_id=conversation_id,
            message_id=arguments.source_message_id,
            ctx=ctx,
        )
        pending_proposals = session.scalars(
            select(AppointmentCancellationProposal)
            .where(
                AppointmentCancellationProposal.organization_id
                == ctx.organization_id,
                AppointmentCancellationProposal.conversation_id == conversation_id,
                AppointmentCancellationProposal.appointment_id == appointment.id,
                AppointmentCancellationProposal.status == "pending",
            )
            .with_for_update()
        ).all()
        for pending in pending_proposals:
            pending.status = "expired"
            pending.updated_at = now
        proposal = AppointmentCancellationProposal(
            organization_id=ctx.organization_id,
            conversation_id=conversation.id,
            contact_identity_id=contact.id,
            appointment_id=appointment.id,
            source_message_id=arguments.source_message_id,
            confirmation_token=uuid4(),
            reason=arguments.reason,
            status="pending",
            expires_at=now + PROPOSAL_TTL,
            updated_at=now,
        )
        session.add(proposal)
        session.flush()
        conversation.status = "awaiting_confirmation"
        conversation.updated_at = now
        result = _cancellation_outcome(proposal)
        record_event(
            session,
            ctx=ctx,
            entity_type="appointment_cancellation_proposal",
            entity_id=str(proposal.id),
            action="appointment_cancellation_proposal.created",
            after_state={
                "appointment_id": appointment.id,
                "source_message_id": arguments.source_message_id,
                "reason_recorded": arguments.reason is not None,
            },
        )
        settle_receipt(
            receipt,
            resource_type="appointment_cancellation_proposal",
            resource_id=str(proposal.id),
            outcome_json=result,
        )
    return proposal


def run_propose_cancellation_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: ProposeCancellationArguments,
    ctx: ExecutionContext,
) -> dict:
    outcome = run_idempotent_command(
        session,
        operation=create_cancellation_proposal,
        operation_name="contact_appointments.propose_cancellation",
        key=str(call.idempotency_key),
        ctx=ctx,
        params={"conversation_id": call.conversation_id, **arguments.model_dump()},
        conversation_id=call.conversation_id,
        arguments=arguments,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    return {"proposal": _cancellation_dto(value), "replayed": outcome.replayed}


def confirm_cancellation_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: ConfirmCancellationArguments,
    idempotency: IdempotencyClaim | None = None,
) -> Appointment:
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONTACT_APPOINTMENTS_CANCEL)
        conversation, contact = _load_conversation_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        proposal = session.scalar(
            select(AppointmentCancellationProposal)
            .where(
                AppointmentCancellationProposal.organization_id
                == ctx.organization_id,
                AppointmentCancellationProposal.id == arguments.proposal_id,
                AppointmentCancellationProposal.conversation_id == conversation_id,
                AppointmentCancellationProposal.confirmation_token
                == arguments.confirmation_token,
            )
            .with_for_update()
        )
        if proposal is None:
            raise AppError(ErrorCode.NOT_FOUND, "Cancellation proposal not found.")
        confirmation_message = _require_inbound_message(
            session,
            conversation_id=conversation_id,
            message_id=arguments.source_message_id,
            ctx=ctx,
        )
        original_message = _require_inbound_message(
            session,
            conversation_id=conversation_id,
            message_id=proposal.source_message_id,
            ctx=ctx,
        )
        if (
            confirmation_message.id == original_message.id
            or confirmation_message.created_at <= original_message.created_at
        ):
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "Cancellation requires a later inbound confirmation message.",
            )
        appointment = _own_appointment(
            session,
            appointment_id=proposal.appointment_id,
            contact=contact,
            ctx=ctx,
            for_update=True,
        )
        if proposal.status == "confirmed":
            settle_receipt(
                receipt,
                resource_type="appointment",
                resource_id=str(appointment.id),
                outcome_json=_appointment_outcome(appointment),
            )
            return appointment
        if proposal.status != "pending" or proposal.expires_at <= now:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The cancellation proposal is no longer confirmable.",
            )
        _require_confirmed(appointment)
        before = _appointment_outcome(appointment)
        appointment.state = "cancelled"
        proposal.status = "confirmed"
        proposal.updated_at = now
        conversation.status = "open"
        conversation.updated_at = now
        session.flush()
        after = _appointment_outcome(appointment)
        record_event(
            session,
            ctx=ctx,
            entity_type="appointment",
            entity_id=str(appointment.id),
            action="appointment.cancelled",
            before_state=before,
            after_state={
                **after,
                "proposal_id": proposal.id,
                "confirmation_message_id": confirmation_message.id,
                "reason_recorded": proposal.reason is not None,
            },
        )
        settle_receipt(
            receipt,
            resource_type="appointment",
            resource_id=str(appointment.id),
            outcome_json=after,
        )
    return appointment


def run_confirm_cancellation_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: ConfirmCancellationArguments,
    ctx: ExecutionContext,
) -> dict:
    outcome = run_idempotent_command(
        session,
        operation=confirm_cancellation_proposal,
        operation_name="contact_appointments.confirm_cancellation",
        key=str(call.idempotency_key),
        ctx=ctx,
        params={
            "conversation_id": call.conversation_id,
            **arguments.model_dump(mode="json"),
        },
        conversation_id=call.conversation_id,
        arguments=arguments,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    appointment = _appointment_dto(value)
    return {
        "appointment": appointment,
        "calendar_action": {"action": "delete", "appointment_id": appointment["id"]},
        "replayed": outcome.replayed,
    }


def _reschedule_outcome(proposal: AppointmentRescheduleProposal) -> dict:
    return {
        "status": proposal.status,
        "resource_type": "appointment_reschedule_proposal",
        "resource_id": str(proposal.id),
        "proposal_id": proposal.id,
        "appointment_id": proposal.appointment_id,
        "confirmation_token": str(proposal.confirmation_token),
        "old_start": proposal.old_start_utc.astimezone(UTC).isoformat(),
        "old_end": proposal.old_end_utc.astimezone(UTC).isoformat(),
        "new_start": proposal.new_start_utc.astimezone(UTC).isoformat(),
        "new_end": proposal.new_end_utc.astimezone(UTC).isoformat(),
        "expires_at": proposal.expires_at.astimezone(UTC).isoformat(),
    }


def _proposal_dto(value: AppointmentRescheduleProposal | dict) -> dict:
    outcome = _reschedule_outcome(value) if isinstance(value, AppointmentRescheduleProposal) else value
    return {
        "id": int(outcome.get("proposal_id", outcome["resource_id"])),
        "appointment_id": outcome["appointment_id"],
        "confirmation_token": outcome["confirmation_token"],
        "old_start": outcome["old_start"],
        "old_end": outcome["old_end"],
        "new_start": outcome["new_start"],
        "new_end": outcome["new_end"],
        "expires_at": outcome["expires_at"],
        "status": outcome["status"],
    }


def _assert_reschedule_slot(
    session: Session,
    *,
    appointment: Appointment,
    new_start: datetime,
    ctx: ExecutionContext,
) -> tuple[datetime, Service, Location]:
    new_start_utc = new_start.astimezone(UTC)
    service = _load_active_scoped(
        session, Service, appointment.service_id, ctx.organization_id, "Service"
    )
    location = _load_active_scoped(
        session, Location, appointment.location_id, ctx.organization_id, "Location"
    )
    _load_active_member(session, appointment.practitioner_id, ctx.organization_id)
    _require_capability(
        session,
        appointment.practitioner_id,
        appointment.service_id,
        appointment.location_id,
        ctx.organization_id,
    )
    new_end_utc = new_start_utc + timedelta(minutes=service.duration_minutes)
    rules, blocks, appointments = _availability_inputs(
        session,
        appointment.practitioner_id,
        appointment.location_id,
        new_start_utc,
        new_end_utc,
        ctx.organization_id,
        exclude_appointment_id=appointment.id,
    )
    bookable = generate_slots(
        rules,
        blocks,
        appointments,
        service.duration_minutes,
        new_start_utc,
        new_end_utc,
        location.timezone,
    )
    if (new_start_utc, new_end_utc) not in bookable:
        raise AppError(
            ErrorCode.SLOT_BLOCKED,
            "The requested interval is not a bookable slot for this practitioner.",
        )
    return new_end_utc, service, location


def create_reschedule_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: ProposeRescheduleArguments,
    idempotency: IdempotencyClaim | None = None,
) -> AppointmentRescheduleProposal:
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONTACT_APPOINTMENTS_RESCHEDULE)
        conversation, contact = _load_conversation_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        appointment = _own_appointment(
            session,
            appointment_id=arguments.appointment_id,
            contact=contact,
            ctx=ctx,
            for_update=True,
        )
        _require_confirmed(appointment)
        new_start_utc = arguments.new_start.astimezone(UTC)
        new_end_utc, _service, _location = _assert_reschedule_slot(
            session, appointment=appointment, new_start=new_start_utc, ctx=ctx
        )
        previous = list(
            session.scalars(
                select(AppointmentRescheduleProposal)
                .where(
                    AppointmentRescheduleProposal.organization_id == ctx.organization_id,
                    AppointmentRescheduleProposal.conversation_id == conversation_id,
                    AppointmentRescheduleProposal.status == "pending",
                )
                .with_for_update()
            )
        )
        for pending in previous:
            pending.status = "expired"
            pending.updated_at = now
        proposal = AppointmentRescheduleProposal(
            organization_id=ctx.organization_id,
            conversation_id=conversation.id,
            contact_identity_id=contact.id,
            appointment_id=appointment.id,
            old_start_utc=appointment.start_utc,
            old_end_utc=appointment.end_utc,
            new_start_utc=new_start_utc,
            new_end_utc=new_end_utc,
            confirmation_token=uuid4(),
            status="pending",
            expires_at=now + PROPOSAL_TTL,
            updated_at=now,
        )
        session.add(proposal)
        session.flush()
        conversation.status = "awaiting_confirmation"
        conversation.updated_at = now
        result = _reschedule_outcome(proposal)
        record_event(
            session,
            ctx=ctx,
            entity_type="appointment_reschedule_proposal",
            entity_id=str(proposal.id),
            action="appointment_reschedule_proposal.created",
            after_state={
                "appointment_id": appointment.id,
                "new_start": new_start_utc.isoformat(),
                "new_end": new_end_utc.isoformat(),
            },
        )
        settle_receipt(
            receipt,
            resource_type="appointment_reschedule_proposal",
            resource_id=str(proposal.id),
            outcome_json=result,
        )
    return proposal


def run_propose_reschedule_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: ProposeRescheduleArguments,
    ctx: ExecutionContext,
) -> dict:
    outcome = run_idempotent_command(
        session,
        operation=create_reschedule_proposal,
        operation_name="contact_appointments.propose_reschedule",
        key=str(call.idempotency_key),
        ctx=ctx,
        params={"conversation_id": call.conversation_id, **arguments.model_dump()},
        conversation_id=call.conversation_id,
        arguments=arguments,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    return {"proposal": _proposal_dto(value), "replayed": outcome.replayed}


def confirm_reschedule_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: ConfirmRescheduleArguments,
    idempotency: IdempotencyClaim | None = None,
) -> Appointment:
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONTACT_APPOINTMENTS_RESCHEDULE)
        conversation, contact = _load_conversation_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        proposal = session.scalar(
            select(AppointmentRescheduleProposal)
            .where(
                AppointmentRescheduleProposal.organization_id == ctx.organization_id,
                AppointmentRescheduleProposal.id == arguments.proposal_id,
                AppointmentRescheduleProposal.conversation_id == conversation_id,
                AppointmentRescheduleProposal.confirmation_token
                == arguments.confirmation_token,
            )
            .with_for_update()
        )
        if proposal is None:
            raise AppError(ErrorCode.NOT_FOUND, "Reschedule proposal not found.")
        appointment = _own_appointment(
            session,
            appointment_id=proposal.appointment_id,
            contact=contact,
            ctx=ctx,
            for_update=True,
        )
        if proposal.status == "confirmed":
            settle_receipt(
                receipt,
                resource_type="appointment",
                resource_id=str(appointment.id),
                outcome_json=_appointment_outcome(appointment),
            )
            return appointment
        if proposal.status != "pending" or proposal.expires_at <= now:
            raise AppError(
                ErrorCode.INVALID_INPUT, "The reschedule proposal is no longer confirmable."
            )
        _require_confirmed(appointment)
        if (
            appointment.start_utc != proposal.old_start_utc
            or appointment.end_utc != proposal.old_end_utc
        ):
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The appointment changed; request a new reschedule proposal.",
            )
        new_end_utc, service, location = _assert_reschedule_slot(
            session,
            appointment=appointment,
            new_start=proposal.new_start_utc,
            ctx=ctx,
        )
        if new_end_utc != proposal.new_end_utc:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The service duration changed; request a new reschedule proposal.",
            )
        before = _appointment_outcome(appointment)
        appointment.start_utc = proposal.new_start_utc
        appointment.end_utc = proposal.new_end_utc
        session.flush()
        proposal.status = "confirmed"
        proposal.updated_at = now
        conversation.status = "open"
        conversation.updated_at = now
        after = _appointment_outcome(appointment)
        record_event(
            session,
            ctx=ctx,
            entity_type="appointment",
            entity_id=str(appointment.id),
            action="appointment.rescheduled",
            before_state=before,
            after_state=after,
        )
        settle_receipt(
            receipt,
            resource_type="appointment",
            resource_id=str(appointment.id),
            outcome_json=after,
        )
    return appointment


def run_confirm_reschedule_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: ConfirmRescheduleArguments,
    ctx: ExecutionContext,
) -> dict:
    outcome = run_idempotent_command(
        session,
        operation=confirm_reschedule_proposal,
        operation_name="contact_appointments.confirm_reschedule",
        key=str(call.idempotency_key),
        ctx=ctx,
        params={"conversation_id": call.conversation_id, **arguments.model_dump(mode="json")},
        conversation_id=call.conversation_id,
        arguments=arguments,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    appointment = _appointment_dto(value)
    service = session.get(Service, appointment["service_id"])
    location = session.get(Location, appointment["location_id"])
    if service is None or location is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment configuration not found.")
    zone = ZoneInfo(location.timezone)
    start = datetime.fromisoformat(appointment["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(appointment["end"].replace("Z", "+00:00"))
    return {
        "appointment": appointment,
        "calendar_action": {
            "action": "update",
            "appointment_id": appointment["id"],
            "summary": f"Cita dental - {service.name}",
            "start": start.astimezone(zone).isoformat(),
            "end": end.astimezone(zone).isoformat(),
            "timezone": location.timezone,
        },
        "replayed": outcome.replayed,
    }


def _handoff_outcome(handoff: ReceptionHandoff) -> dict:
    return {
        "status": handoff.status,
        "resource_type": "reception_handoff",
        "resource_id": str(handoff.id),
        "handoff_id": handoff.id,
        "conversation_id": handoff.conversation_id,
        "reason_code": handoff.reason_code,
        "reason_summary": handoff.reason_summary,
    }


def request_handoff(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: HumanHandoffArguments,
    idempotency: IdempotencyClaim | None = None,
) -> ReceptionHandoff:
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONVERSATIONS_MANAGE)
        conversation, contact = _load_conversation_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        handoff = session.scalar(
            select(ReceptionHandoff)
            .where(
                ReceptionHandoff.organization_id == ctx.organization_id,
                ReceptionHandoff.conversation_id == conversation_id,
                ReceptionHandoff.status.in_(("pending", "claimed")),
            )
            .with_for_update()
        )
        if handoff is None:
            handoff = ReceptionHandoff(
                organization_id=ctx.organization_id,
                conversation_id=conversation.id,
                contact_identity_id=contact.id,
                reason_code=arguments.reason_code,
                reason_summary=arguments.reason_summary,
                status="pending",
                updated_at=now,
            )
            session.add(handoff)
            session.flush()
        conversation.status = "human_handoff"
        conversation.updated_at = now
        result = _handoff_outcome(handoff)
        record_event(
            session,
            ctx=ctx,
            entity_type="conversation",
            entity_id=str(conversation.id),
            action="conversation.human_handoff_requested",
            after_state={
                "handoff_id": handoff.id,
                "reason_code": handoff.reason_code,
                "status": handoff.status,
            },
        )
        settle_receipt(
            receipt,
            resource_type="reception_handoff",
            resource_id=str(handoff.id),
            outcome_json=result,
        )
    return handoff


def run_handoff_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: HumanHandoffArguments,
    ctx: ExecutionContext,
) -> dict:
    outcome = run_idempotent_command(
        session,
        operation=request_handoff,
        operation_name="conversations.request_handoff",
        key=str(call.idempotency_key),
        ctx=ctx,
        params={"conversation_id": call.conversation_id, **arguments.model_dump()},
        conversation_id=call.conversation_id,
        arguments=arguments,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    handoff = _handoff_outcome(value) if isinstance(value, ReceptionHandoff) else value
    return {"handoff": handoff, "replayed": outcome.replayed}


def resume_automation(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    arguments: EmptyArguments,
    idempotency: IdempotencyClaim | None = None,
) -> dict:
    del arguments
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONVERSATIONS_RESUME)
        conversation, _contact = _load_conversation_contact(
            session,
            conversation_id=conversation_id,
            ctx=ctx,
            for_update=True,
            allow_human_handoff=True,
        )
        handoffs = session.scalars(
            select(ReceptionHandoff)
            .where(
                ReceptionHandoff.organization_id == ctx.organization_id,
                ReceptionHandoff.conversation_id == conversation_id,
                ReceptionHandoff.status.in_(("pending", "claimed")),
            )
            .with_for_update()
        ).all()
        for handoff in handoffs:
            handoff.status = "resolved"
            handoff.updated_at = now
        conversation.status = "open"
        conversation.updated_at = now
        result = {
            "status": "open",
            "resource_type": "conversation",
            "resource_id": str(conversation.id),
            "conversation_id": conversation.id,
            "resolved_handoff_ids": [handoff.id for handoff in handoffs],
        }
        record_event(
            session,
            ctx=ctx,
            entity_type="conversation",
            entity_id=str(conversation.id),
            action="conversation.automation_resumed",
            after_state={
                "status": conversation.status,
                "resolved_handoff_ids": result["resolved_handoff_ids"],
            },
        )
        settle_receipt(
            receipt,
            resource_type="conversation",
            resource_id=str(conversation.id),
            outcome_json=result,
        )
    return result
