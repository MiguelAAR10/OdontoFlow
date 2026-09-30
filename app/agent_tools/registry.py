"""The single source of truth for agent tools (B1).

Adding a tool means adding one ``ToolSpec`` here. ``service.call_agent_tool``
dispatches through ``TOOL_REGISTRY`` and enforces the server-side allowlist in
``AGENT_DEFINITIONS``; ``READ_TOOL_NAMES``/``MUTATION_TOOL_NAMES`` and
``service.ARGUMENT_MODELS`` are derived from it.

``ToolName`` (the envelope ``Literal``) stays in ``schemas.py`` because the
envelope must be static for Pydantic/OpenAPI; ``tests/test_tool_registry.py``
fails if it drifts from this registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent_tools.booking import (
    run_confirm_appointment_tool,
    run_propose_appointment_tool,
)
from app.agent_tools.guards import require_automation_active
from app.agent_tools.reception import (
    contact_profile,
    reception_context,
    run_confirm_cancellation_tool,
    run_confirm_reschedule_tool,
    run_handoff_tool,
    run_propose_cancellation_tool,
    run_propose_reschedule_tool,
    run_register_contact_profile_tool,
)
from app.agent_tools.schemas import (
    AgentToolCall,
    AppointmentArguments,
    AvailableSlotsArguments,
    ConfirmAppointmentArguments,
    ConfirmCancellationArguments,
    ConfirmRescheduleArguments,
    ContactAppointmentsArguments,
    EligiblePractitionersArguments,
    EmptyArguments,
    HumanHandoffArguments,
    ProposeAppointmentArguments,
    ProposeCancellationArguments,
    ProposeRescheduleArguments,
    ReceptionContextArguments,
    RegisterContactProfileArguments,
)
from app.catalog.service import list_services
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.models import Principal
from app.iam.permissions import (
    AVAILABILITY_READ,
    CONTACT_APPOINTMENTS_BOOK,
    CONTACT_APPOINTMENTS_CANCEL,
    CONTACT_APPOINTMENTS_READ,
    CONTACT_APPOINTMENTS_RESCHEDULE,
    CONTACT_PROFILES_MANAGE,
    CONVERSATIONS_MANAGE,
    CONVERSATIONS_READ,
    LOCATIONS_READ,
    PRACTITIONERS_READ,
    SERVICES_READ,
)
from app.iam.service import require_permission
from app.messaging.models import ContactIdentity, Conversation
from app.organization.service import list_eligible_practitioners, list_locations
from app.scheduling.models import Appointment
from app.scheduling.query import find_available_slots

Effect = Literal["read", "propose", "execute"]
Level = Literal["L0", "L1", "L2", "L3", "L4"]
ToolHandler = Callable[..., dict[str, Any]]

MAX_SLOT_WINDOW = timedelta(days=14)
MAX_TOOL_ROWS = 100
STATEMENT_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class ToolSpec:
    """One agent tool: contract, handler and risk classification.

    ``handler(session, *, call, arguments, ctx) -> dict``. ``permissions`` is
    documentation (the handler enforces them); the registry test checks that
    every code exists in the permission catalog.
    """

    name: str
    args_model: type[BaseModel]
    handler: ToolHandler
    effect: Effect
    level: Level
    permissions: tuple[str, ...]
    needs_conversation: bool
    description: str


# --- read handlers ----------------------------------------------------------


def _set_statement_timeout(session: Session) -> None:
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": str(STATEMENT_TIMEOUT_MS)},
    )


def _load_conversation_contact(
    session: Session,
    *,
    conversation_id: int,
    ctx: ExecutionContext,
) -> tuple[Conversation, ContactIdentity]:
    require_permission(session, ctx, CONVERSATIONS_READ)
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.organization_id == ctx.organization_id,
            Conversation.id == conversation_id,
        )
    )
    if conversation is None:
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


def _appointment_dto(appointment: Appointment) -> dict:
    return {
        "id": appointment.id,
        "service_id": appointment.service_id,
        "practitioner_id": appointment.practitioner_id,
        "location_id": appointment.location_id,
        "start": appointment.start_utc,
        "end": appointment.end_utc,
        "state": appointment.state,
    }


def _contact_appointments_statement(*, contact: ContactIdentity, ctx: ExecutionContext):
    if contact.lead_id is None:
        return None
    return select(Appointment).where(
        Appointment.organization_id == ctx.organization_id,
        Appointment.lead_id == contact.lead_id,
    )


def _read_tool(body: Callable[..., dict]) -> ToolHandler:
    """Wrap a read body with the shared timeout + conversation/contact scope."""

    def handler(
        session: Session, *, call: AgentToolCall, arguments: BaseModel, ctx: ExecutionContext
    ) -> dict:
        _set_statement_timeout(session)
        conversation, contact = _load_conversation_contact(
            session, conversation_id=call.conversation_id, ctx=ctx
        )
        return body(
            session, arguments=arguments, conversation=conversation, contact=contact, ctx=ctx
        )

    return handler


def _get_reception_context(session, *, arguments, conversation, contact, ctx):
    return reception_context(
        session, arguments=arguments, conversation=conversation, contact=contact, ctx=ctx
    )


def _get_contact_profile(session, *, arguments, conversation, contact, ctx):
    return {"profile": contact_profile(session, contact=contact, ctx=ctx)}


def _list_services(session, *, arguments, conversation, contact, ctx):
    services = [service for service in list_services(session, ctx=ctx) if service.is_active]
    return {
        "services": [
            {"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes}
            for s in services
        ]
    }


def _list_locations(session, *, arguments, conversation, contact, ctx):
    locations = [
        location for location in list_locations(session, ctx=ctx) if location.is_active
    ]
    return {
        "locations": [
            {"id": loc.id, "name": loc.name, "timezone": loc.timezone} for loc in locations
        ]
    }


def _list_eligible_practitioners(session, *, arguments, conversation, contact, ctx):
    practitioners = list_eligible_practitioners(
        session,
        service_id=arguments.service_id,
        location_id=arguments.location_id,
        ctx=ctx,
    )
    return {
        "practitioners": [
            {"id": p.id, "display_name": p.display_name}
            for p in practitioners[:MAX_TOOL_ROWS]
        ]
    }


def _query_available_slots(session, *, arguments, conversation, contact, ctx):
    if arguments.window_end - arguments.window_start > MAX_SLOT_WINDOW:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Availability queries are limited to a 14-day window.",
        )
    slots = find_available_slots(
        session,
        service_id=arguments.service_id,
        location_id=arguments.location_id,
        window_start=arguments.window_start,
        window_end=arguments.window_end,
        ctx=ctx,
    )
    return {"slots": slots[:MAX_TOOL_ROWS]}


def _get_appointment(session, *, arguments, conversation, contact, ctx):
    require_permission(session, ctx, CONTACT_APPOINTMENTS_READ)
    statement = _contact_appointments_statement(contact=contact, ctx=ctx)
    if statement is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
    appointment = session.scalar(statement.where(Appointment.id == arguments.appointment_id))
    if appointment is None:
        raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
    return {"appointment": _appointment_dto(appointment)}


def _list_contact_appointments(session, *, arguments, conversation, contact, ctx):
    require_permission(session, ctx, CONTACT_APPOINTMENTS_READ)
    statement = _contact_appointments_statement(contact=contact, ctx=ctx)
    if statement is None:
        return {"appointments": []}
    if arguments.from_date is not None:
        statement = statement.where(Appointment.end_utc > arguments.from_date)
    if arguments.to_date is not None:
        statement = statement.where(Appointment.start_utc < arguments.to_date)
    statement = statement.order_by(Appointment.start_utc).limit(MAX_TOOL_ROWS)
    return {
        "appointments": [_appointment_dto(a) for a in session.scalars(statement)]
    }


# --- the registry -----------------------------------------------------------


def _spec(name, args_model, handler, effect, level, permissions, description) -> ToolSpec:
    return ToolSpec(
        name=name,
        args_model=args_model,
        handler=handler,
        effect=effect,
        level=level,
        permissions=tuple(permissions),
        # Every current tool is scoped to (and loads) one conversation.
        needs_conversation=True,
        description=description,
    )


_CONV = (CONVERSATIONS_READ,)

TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        # L0 / read
        _spec("list_services", EmptyArguments, _read_tool(_list_services), "read", "L0",
              _CONV + (SERVICES_READ,), "List active bookable services."),
        _spec("list_locations", EmptyArguments, _read_tool(_list_locations), "read", "L0",
              _CONV + (LOCATIONS_READ,), "List active clinic locations."),
        _spec("list_eligible_practitioners", EligiblePractitionersArguments,
              _read_tool(_list_eligible_practitioners), "read", "L0",
              _CONV + (PRACTITIONERS_READ,),
              "List practitioners able to deliver a service at a location."),
        _spec("query_available_slots", AvailableSlotsArguments,
              _read_tool(_query_available_slots), "read", "L0",
              _CONV + (AVAILABILITY_READ,),
              "Query deterministic available slots (window of at most 14 days)."),
        _spec("get_appointment", AppointmentArguments, _read_tool(_get_appointment),
              "read", "L0", _CONV + (CONTACT_APPOINTMENTS_READ,),
              "Read one appointment of the conversation's contact."),
        _spec("list_contact_appointments", ContactAppointmentsArguments,
              _read_tool(_list_contact_appointments), "read", "L0",
              _CONV + (CONTACT_APPOINTMENTS_READ,),
              "List the conversation contact's appointments."),
        _spec("get_reception_context", ReceptionContextArguments,
              _read_tool(_get_reception_context), "read", "L0",
              _CONV + (SERVICES_READ, LOCATIONS_READ, CONTACT_APPOINTMENTS_READ),
              "Read the reception context of the conversation."),
        _spec("get_contact_profile", EmptyArguments, _read_tool(_get_contact_profile),
              "read", "L0", _CONV, "Read the conversation contact's profile."),
        # L1 / execute (internal effects)
        _spec("register_contact_profile", RegisterContactProfileArguments,
              run_register_contact_profile_tool, "execute", "L1",
              (CONTACT_PROFILES_MANAGE,), "Register the contact's patient profile."),
        _spec("request_human_handoff", HumanHandoffArguments, run_handoff_tool,
              "execute", "L1", (CONVERSATIONS_MANAGE,),
              "Transfer the conversation to human reception and stop automation."),
        # L3 / propose (visible to the patient, confirmed by a human)
        _spec("propose_appointment", ProposeAppointmentArguments,
              run_propose_appointment_tool, "propose", "L3",
              (CONTACT_APPOINTMENTS_BOOK,),
              "Create a pending appointment proposal for one exact returned slot."),
        _spec("propose_cancellation", ProposeCancellationArguments,
              run_propose_cancellation_tool, "propose", "L3",
              (CONTACT_APPOINTMENTS_CANCEL,),
              "Create a pending cancellation proposal for an appointment."),
        _spec("propose_reschedule", ProposeRescheduleArguments,
              run_propose_reschedule_tool, "propose", "L3",
              (CONTACT_APPOINTMENTS_RESCHEDULE,),
              "Create a pending reschedule proposal for an appointment."),
        # L4 / execute (never for agent principals)
        _spec("confirm_appointment", ConfirmAppointmentArguments,
              run_confirm_appointment_tool, "execute", "L4",
              (CONTACT_APPOINTMENTS_BOOK,), "Confirm a pending appointment proposal."),
        _spec("confirm_cancellation", ConfirmCancellationArguments,
              run_confirm_cancellation_tool, "execute", "L4",
              (CONTACT_APPOINTMENTS_CANCEL,), "Confirm a pending cancellation proposal."),
        _spec("confirm_reschedule", ConfirmRescheduleArguments,
              run_confirm_reschedule_tool, "execute", "L4",
              (CONTACT_APPOINTMENTS_RESCHEDULE,), "Confirm a pending reschedule proposal."),
    )
}

READ_TOOL_NAMES = frozenset(n for n, s in TOOL_REGISTRY.items() if s.effect == "read")
MUTATION_TOOL_NAMES = frozenset(TOOL_REGISTRY) - READ_TOOL_NAMES

# --- server-side allowlist (config in code; B4 moves it to agent_definitions) --

DEFAULT_AGENT_KEY = "reception"
AGENT_DEFINITIONS: dict[str, frozenset[str]] = {
    "reception": frozenset(n for n, s in TOOL_REGISTRY.items() if s.level != "L4"),
}
#: Principal.display_name -> agent_key. Unmapped agent principals fall back to
#: ``DEFAULT_AGENT_KEY`` (compatibility debt, B4).
AGENT_KEY_BY_DISPLAY_NAME: dict[str, str] = {
    "n8n-lab-agent": "reception",
    "local-sales-agent-v0": "reception",
}


def resolve_agent_key(session: Session, ctx: ExecutionContext) -> str | None:
    """Return the caller's agent_key, or ``None`` for non-agent principals.

    Reads ``Principal.display_name``; the caller owns ending that read
    transaction before a mutation handler opens its own.
    """
    if ctx.principal_type != "agent":
        return None
    display_name = session.scalar(
        select(Principal.display_name).where(Principal.id == ctx.principal_id)
    )
    return AGENT_KEY_BY_DISPLAY_NAME.get(display_name or "", DEFAULT_AGENT_KEY)


def allowed_tools(agent_key: str | None) -> frozenset[str]:
    """Tools the caller may invoke; L4 is removed for every agent_key."""
    if agent_key is None:
        return frozenset(TOOL_REGISTRY)
    listed = AGENT_DEFINITIONS.get(agent_key, frozenset())
    return frozenset(t for t in listed if t in TOOL_REGISTRY and TOOL_REGISTRY[t].level != "L4")


__all__ = [
    "AGENT_DEFINITIONS",
    "AGENT_KEY_BY_DISPLAY_NAME",
    "DEFAULT_AGENT_KEY",
    "MUTATION_TOOL_NAMES",
    "READ_TOOL_NAMES",
    "TOOL_REGISTRY",
    "ToolSpec",
    "allowed_tools",
    "resolve_agent_key",
]
