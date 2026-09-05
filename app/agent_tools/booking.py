"""Persisted confirmation commands for contact-bound reception booking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_tools.schemas import (
    AgentToolCall,
    ConfirmAppointmentArguments,
    ProposeAppointmentArguments,
)
from app.agent_tools.guards import require_automation_active
from app.agent_tools.reception import ensure_contact_profile
from app.audit.service import record_event
from app.catalog.models import Service
from app.commercial.models import Lead
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import CONTACT_APPOINTMENTS_BOOK
from app.iam.service import require_permission
from app.idempotency.service import (
    IdempotencyClaim,
    claim_receipt,
    run_idempotent_command,
    settle_receipt,
)
from app.messaging.models import ContactIdentity, Conversation
from app.organization.models import Location, Practitioner
from app.scheduling.models import Appointment, AppointmentProposal
from app.scheduling.query import find_available_slots
from app.scheduling.service import (
    _appointment_outcome,
    _book_appointment_core,
    _require_aware,
)

PROPOSAL_TTL = timedelta(minutes=15)
PROPOSAL_ENTITY_TYPE = "appointment_proposal"
PROPOSAL_CREATED_ACTION = "appointment_proposal.created"
PROPOSAL_CONFIRMED_ACTION = "appointment_proposal.confirmed"
OP_PROPOSAL_CREATE = "contact_appointments.propose"
OP_PROPOSAL_CONFIRM = "contact_appointments.confirm"


def _load_conversation_and_contact(
    session: Session,
    *,
    conversation_id: int,
    ctx: ExecutionContext,
    for_update: bool = False,
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


def _proposal_outcome(proposal: AppointmentProposal) -> dict:
    return {
        "status": proposal.status,
        "resource_type": PROPOSAL_ENTITY_TYPE,
        "resource_id": str(proposal.id),
        "proposal_id": proposal.id,
        "confirmation_token": str(proposal.confirmation_token),
        "service_id": proposal.service_id,
        "patient_id": proposal.patient_id,
        "location_id": proposal.location_id,
        "practitioner_id": proposal.practitioner_id,
        "start": proposal.start_utc.astimezone(UTC).isoformat(),
        "end": proposal.end_utc.astimezone(UTC).isoformat(),
        "expires_at": proposal.expires_at.astimezone(UTC).isoformat(),
    }


def _proposal_dto(value: AppointmentProposal | dict) -> dict:
    outcome = _proposal_outcome(value) if isinstance(value, AppointmentProposal) else value
    return {
        "id": int(outcome.get("proposal_id", outcome["resource_id"])),
        "confirmation_token": outcome["confirmation_token"],
        "service_id": outcome["service_id"],
        "location_id": outcome["location_id"],
        "practitioner_id": outcome["practitioner_id"],
        "start": datetime.fromisoformat(outcome["start"]).astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        ),
        "end": datetime.fromisoformat(outcome["end"]).astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        ),
        "expires_at": outcome["expires_at"],
        "status": outcome["status"],
    }


def create_contact_booking_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    full_name: str,
    service_id: int,
    location_id: int,
    practitioner_id: int,
    start: datetime,
    idempotency: IdempotencyClaim | None = None,
) -> AppointmentProposal:
    """Persist one exact slot for later explicit confirmation."""
    start_utc = _require_aware(start)
    normalized_name = " ".join(full_name.split())
    if len(normalized_name) < 2:
        raise AppError(ErrorCode.INVALID_INPUT, "A patient name is required.")
    now = datetime.now(UTC)

    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(
            session,
            ctx,
            CONTACT_APPOINTMENTS_BOOK,
            location_id=location_id,
        )
        conversation, contact = _load_conversation_and_contact(
            session, conversation_id=conversation_id, ctx=ctx, for_update=True
        )
        service = session.scalar(
            select(Service).where(
                Service.organization_id == ctx.organization_id,
                Service.id == service_id,
                Service.is_active.is_(True),
            )
        )
        if service is None:
            raise AppError(ErrorCode.NOT_FOUND, "Service not found.")
        if service.booking_mode != "automatic":
            next_step = (
                "Book an automatic evaluation service first."
                if service.booking_mode == "evaluation_first"
                else "Request a human handoff for this service."
            )
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "This service cannot be booked directly by reception.",
                details={
                    "booking_mode": service.booking_mode,
                    "required_next_step": next_step,
                },
            )
        end_utc = start_utc + timedelta(minutes=service.duration_minutes)
        slots = find_available_slots(
            session,
            service_id=service_id,
            location_id=location_id,
            window_start=start_utc,
            window_end=end_utc,
            ctx=ctx,
        )
        if not any(
            slot["practitioner_id"] == practitioner_id
            and slot["start"] == start_utc
            and slot["end"] == end_utc
            for slot in slots
        ):
            raise AppError(
                ErrorCode.SLOT_BLOCKED,
                "The requested interval is not a bookable slot for this practitioner.",
            )

        patient, lead = ensure_contact_profile(
            session,
            contact=contact,
            full_name=normalized_name,
            ctx=ctx,
        )
        lead.service_need_id = service_id

        previous = list(
            session.scalars(
                select(AppointmentProposal)
                .where(
                    AppointmentProposal.organization_id == ctx.organization_id,
                    AppointmentProposal.conversation_id == conversation_id,
                    AppointmentProposal.status == "pending",
                )
                .with_for_update()
            )
        )
        for proposal in previous:
            proposal.status = "expired"
            proposal.updated_at = now

        proposal = AppointmentProposal(
            organization_id=ctx.organization_id,
            conversation_id=conversation.id,
            contact_identity_id=contact.id,
            lead_id=lead.id,
            patient_id=patient.id,
            service_id=service_id,
            practitioner_id=practitioner_id,
            location_id=location_id,
            full_name=normalized_name,
            start_utc=start_utc,
            end_utc=end_utc,
            confirmation_token=uuid4(),
            status="pending",
            expires_at=now + PROPOSAL_TTL,
            updated_at=now,
        )
        session.add(proposal)
        session.flush()
        conversation.status = "awaiting_confirmation"
        conversation.updated_at = now
        record_event(
            session,
            ctx=ctx,
            entity_type=PROPOSAL_ENTITY_TYPE,
            entity_id=str(proposal.id),
            action=PROPOSAL_CREATED_ACTION,
            after_state={
                "conversation_id": conversation.id,
                "service_id": service_id,
                "location_id": location_id,
                "practitioner_id": practitioner_id,
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
                "status": "pending",
                "expires_at": proposal.expires_at.isoformat(),
            },
        )
        settle_receipt(
            receipt,
            resource_type=PROPOSAL_ENTITY_TYPE,
            resource_id=str(proposal.id),
            outcome_json=_proposal_outcome(proposal),
        )
    return proposal


def confirm_contact_booking_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    proposal_id: int,
    confirmation_token: UUID,
    idempotency: IdempotencyClaim | None = None,
) -> Appointment:
    """Atomically consume a confirmed proposal and create the appointment."""
    now = datetime.now(UTC)
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONTACT_APPOINTMENTS_BOOK)
        proposal = session.scalar(
            select(AppointmentProposal)
            .where(
                AppointmentProposal.organization_id == ctx.organization_id,
                AppointmentProposal.id == proposal_id,
                AppointmentProposal.conversation_id == conversation_id,
                AppointmentProposal.confirmation_token == confirmation_token,
            )
            .with_for_update()
        )
        if proposal is None:
            raise AppError(ErrorCode.NOT_FOUND, "Appointment proposal not found.")
        if proposal.status == "confirmed" and proposal.appointment_id is not None:
            appointment = session.scalar(
                select(Appointment).where(
                    Appointment.organization_id == ctx.organization_id,
                    Appointment.id == proposal.appointment_id,
                )
            )
            if appointment is None:
                raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
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
                "The appointment proposal is no longer confirmable.",
            )

        appointment = _book_appointment_core(
            session,
            resolved=ctx,
            lead_id=proposal.lead_id,
            service_id=proposal.service_id,
            location_id=proposal.location_id,
            practitioner_id=proposal.practitioner_id,
            start_utc=proposal.start_utc.astimezone(UTC),
        )
        appointment.patient_id = proposal.patient_id
        if appointment.end_utc != proposal.end_utc:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The service duration changed; request a new appointment proposal.",
            )
        proposal.status = "confirmed"
        proposal.appointment_id = appointment.id
        proposal.updated_at = now
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.organization_id == ctx.organization_id,
                Conversation.id == conversation_id,
            )
        )
        if conversation is not None:
            conversation.status = "open"
            conversation.updated_at = now
        record_event(
            session,
            ctx=ctx,
            entity_type=PROPOSAL_ENTITY_TYPE,
            entity_id=str(proposal.id),
            action=PROPOSAL_CONFIRMED_ACTION,
            before_state={"status": "pending"},
            after_state={
                "status": "confirmed",
                "appointment_id": appointment.id,
            },
        )
        settle_receipt(
            receipt,
            resource_type="appointment",
            resource_id=str(appointment.id),
            outcome_json=_appointment_outcome(appointment),
        )
    return appointment


def run_propose_appointment_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: ProposeAppointmentArguments,
    ctx: ExecutionContext,
) -> dict:
    params = {
        "conversation_id": call.conversation_id,
        "full_name": arguments.full_name,
        "service_id": arguments.service_id,
        "location_id": arguments.location_id,
        "practitioner_id": arguments.practitioner_id,
        "start": arguments.start,
    }
    outcome = run_idempotent_command(
        session,
        operation=create_contact_booking_proposal,
        operation_name=OP_PROPOSAL_CREATE,
        key=str(call.idempotency_key),
        ctx=ctx,
        params=params,
        **params,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    return {"proposal": _proposal_dto(value), "replayed": outcome.replayed}


def _appointment_payload(value: Appointment | dict) -> dict:
    if isinstance(value, Appointment):
        return {
            "id": value.id,
            "service_id": value.service_id,
            "patient_id": value.patient_id,
            "practitioner_id": value.practitioner_id,
            "location_id": value.location_id,
            "start": value.start_utc,
            "end": value.end_utc,
            "state": value.state,
        }
    return {
        "id": int(value["resource_id"]),
        "service_id": value["service_id"],
        "patient_id": value.get("patient_id"),
        "practitioner_id": value["practitioner_id"],
        "location_id": value["location_id"],
        "start": datetime.fromisoformat(value["start_utc"]),
        "end": datetime.fromisoformat(value["end_utc"]),
        "state": value["state"],
    }


def run_confirm_appointment_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: ConfirmAppointmentArguments,
    ctx: ExecutionContext,
) -> dict:
    params = {
        "conversation_id": call.conversation_id,
        "proposal_id": arguments.proposal_id,
        "confirmation_token": arguments.confirmation_token,
    }
    outcome = run_idempotent_command(
        session,
        operation=confirm_contact_booking_proposal,
        operation_name=OP_PROPOSAL_CONFIRM,
        key=str(call.idempotency_key),
        ctx=ctx,
        params=params,
        **params,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    appointment = _appointment_payload(value)
    service = session.get(Service, appointment["service_id"])
    location = session.get(Location, appointment["location_id"])
    practitioner = session.get(Practitioner, appointment["practitioner_id"])
    if service is None or location is None or practitioner is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment configuration not found.")
    timezone = ZoneInfo(location.timezone)
    start_local = appointment["start"].astimezone(timezone)
    end_local = appointment["end"].astimezone(timezone)
    return {
        "appointment": {
            "id": appointment["id"],
            "service_id": appointment["service_id"],
            "patient_id": appointment.get("patient_id"),
            "practitioner_id": appointment["practitioner_id"],
            "location_id": appointment["location_id"],
            "start": appointment["start"].astimezone(UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "end": appointment["end"].astimezone(UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "state": appointment["state"],
        },
        "calendar_event": {
            "summary": f"Cita dental - {service.name}",
            "description": f"OdontoFlow cita #{appointment['id']}",
            "start": start_local.isoformat(),
            "end": end_local.isoformat(),
            "timezone": location.timezone,
        },
        "replayed": outcome.replayed,
    }
