"""Deterministic, contact-safe reception-agent tool gateway."""

from __future__ import annotations

from datetime import timedelta
from time import perf_counter_ns
from typing import TypeAlias

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent_tools.schemas import (
    AgentToolCall,
    AgentToolError,
    AgentToolResult,
    AppointmentArguments,
    AvailableSlotsArguments,
    ConfirmCancellationArguments,
    ConfirmRescheduleArguments,
    ConfirmAppointmentArguments,
    ContactAppointmentsArguments,
    EligiblePractitionersArguments,
    EmptyArguments,
    HumanHandoffArguments,
    MUTATION_TOOL_NAMES,
    ProposeCancellationArguments,
    ProposeRescheduleArguments,
    ProposeAppointmentArguments,
    ReceptionContextArguments,
    RegisterContactProfileArguments,
)
from app.agent_tools.booking import (
    run_confirm_appointment_tool,
    run_propose_appointment_tool,
)
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
from app.agent_tools.guards import require_automation_active
from app.audit.service import record_event
from app.catalog.service import list_services
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import CONTACT_APPOINTMENTS_READ, CONVERSATIONS_READ
from app.iam.service import require_permission
from app.messaging.models import ContactIdentity, Conversation
from app.organization.service import list_eligible_practitioners, list_locations
from app.scheduling.models import Appointment
from app.scheduling.query import find_available_slots

MAX_SLOT_WINDOW = timedelta(days=14)
MAX_TOOL_ROWS = 100
STATEMENT_TIMEOUT_MS = 5_000

ArgumentModel: TypeAlias = type[BaseModel]
ARGUMENT_MODELS: dict[str, ArgumentModel] = {
    "list_services": EmptyArguments,
    "list_locations": EmptyArguments,
    "list_eligible_practitioners": EligiblePractitionersArguments,
    "query_available_slots": AvailableSlotsArguments,
    "get_appointment": AppointmentArguments,
    "list_contact_appointments": ContactAppointmentsArguments,
    "propose_appointment": ProposeAppointmentArguments,
    "confirm_appointment": ConfirmAppointmentArguments,
    "get_reception_context": ReceptionContextArguments,
    "get_contact_profile": EmptyArguments,
    "register_contact_profile": RegisterContactProfileArguments,
    "propose_cancellation": ProposeCancellationArguments,
    "confirm_cancellation": ConfirmCancellationArguments,
    "propose_reschedule": ProposeRescheduleArguments,
    "confirm_reschedule": ConfirmRescheduleArguments,
    "request_human_handoff": HumanHandoffArguments,
}


def _duration_ms(started_ns: int) -> int:
    return max(0, (perf_counter_ns() - started_ns) // 1_000_000)


def _set_statement_timeout(session: Session) -> None:
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": str(STATEMENT_TIMEOUT_MS)},
    )


def _validate_trace(call: AgentToolCall, ctx: ExecutionContext) -> None:
    if str(call.request_id) != ctx.request_id or str(call.correlation_id) != ctx.correlation_id:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Tool envelope trace identifiers must match the HTTP trace headers.",
        )


def _parse_arguments(call: AgentToolCall) -> BaseModel:
    try:
        return ARGUMENT_MODELS[call.tool_name].model_validate(call.arguments)
    except ValidationError:
        raise AppError(ErrorCode.INVALID_INPUT, "The tool arguments are invalid.")


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


def _contact_appointments_statement(
    *,
    contact: ContactIdentity,
    ctx: ExecutionContext,
):
    if contact.lead_id is None:
        return None
    return select(Appointment).where(
        Appointment.organization_id == ctx.organization_id,
        Appointment.lead_id == contact.lead_id,
    )


def _execute_tool(
    session: Session,
    *,
    call: AgentToolCall,
    arguments: BaseModel,
    conversation: Conversation,
    contact: ContactIdentity,
    ctx: ExecutionContext,
) -> dict:
    if call.tool_name == "get_reception_context":
        assert isinstance(arguments, ReceptionContextArguments)
        return reception_context(
            session,
            arguments=arguments,
            conversation=conversation,
            contact=contact,
            ctx=ctx,
        )

    if call.tool_name == "get_contact_profile":
        return {"profile": contact_profile(session, contact=contact, ctx=ctx)}

    if call.tool_name == "list_services":
        services = [service for service in list_services(session, ctx=ctx) if service.is_active]
        return {
            "services": [
                {
                    "id": service.id,
                    "name": service.name,
                    "duration_minutes": service.duration_minutes,
                }
                for service in services
            ]
        }

    if call.tool_name == "list_locations":
        locations = [location for location in list_locations(session, ctx=ctx) if location.is_active]
        return {
            "locations": [
                {
                    "id": location.id,
                    "name": location.name,
                    "timezone": location.timezone,
                }
                for location in locations
            ]
        }

    if call.tool_name == "list_eligible_practitioners":
        assert isinstance(arguments, EligiblePractitionersArguments)
        practitioners = list_eligible_practitioners(
            session,
            service_id=arguments.service_id,
            location_id=arguments.location_id,
            ctx=ctx,
        )
        return {
            "practitioners": [
                {"id": practitioner.id, "display_name": practitioner.display_name}
                for practitioner in practitioners[:MAX_TOOL_ROWS]
            ]
        }

    if call.tool_name == "query_available_slots":
        assert isinstance(arguments, AvailableSlotsArguments)
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

    require_permission(session, ctx, CONTACT_APPOINTMENTS_READ)
    statement = _contact_appointments_statement(contact=contact, ctx=ctx)

    if call.tool_name == "get_appointment":
        assert isinstance(arguments, AppointmentArguments)
        if statement is None:
            raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
        appointment = session.scalar(
            statement.where(Appointment.id == arguments.appointment_id)
        )
        if appointment is None:
            raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
        return {"appointment": _appointment_dto(appointment)}

    assert call.tool_name == "list_contact_appointments"
    assert isinstance(arguments, ContactAppointmentsArguments)
    if statement is None:
        return {"appointments": []}
    if arguments.from_date is not None:
        statement = statement.where(Appointment.end_utc > arguments.from_date)
    if arguments.to_date is not None:
        statement = statement.where(Appointment.start_utc < arguments.to_date)
    statement = statement.order_by(Appointment.start_utc).limit(MAX_TOOL_ROWS)
    return {
        "appointments": [
            _appointment_dto(appointment)
            for appointment in session.scalars(statement)
        ]
    }


def _audit_tool_call(
    session: Session,
    *,
    call: AgentToolCall,
    ctx: ExecutionContext,
    status: str,
    duration_ms: int,
    error_code: str | None = None,
) -> None:
    metadata = {
        "tool_name": call.tool_name,
        "tool_version": call.tool_version,
        "status": status,
        "duration_ms": duration_ms,
    }
    if error_code is not None:
        metadata["error_code"] = error_code
    record_event(
        session,
        ctx=ctx,
        entity_type="agent_tool",
        entity_id=str(call.conversation_id),
        action="agent_tool.called",
        after_state=metadata,
    )


def call_agent_tool(
    session: Session,
    *,
    call: AgentToolCall,
    ctx: ExecutionContext,
) -> AgentToolResult:
    """Execute one allowlisted tool and always return the stable envelope.

    Booking commands deliberately validate their transport-only fields before
    touching PostgreSQL and then enter their command handler directly.  That
    preserves the idempotency invariant that the receipt claim is the first
    database statement of a mutation transaction.
    """
    started_ns = perf_counter_ns()
    try:
        _validate_trace(call, ctx)
        arguments = _parse_arguments(call)
        if call.tool_name in MUTATION_TOOL_NAMES:
            if call.tool_name == "propose_appointment":
                assert isinstance(arguments, ProposeAppointmentArguments)
                data = run_propose_appointment_tool(
                    session,
                    call=call,
                    arguments=arguments,
                    ctx=ctx,
                )
            elif call.tool_name == "confirm_appointment":
                assert isinstance(arguments, ConfirmAppointmentArguments)
                data = run_confirm_appointment_tool(
                    session,
                    call=call,
                    arguments=arguments,
                    ctx=ctx,
                )
            elif call.tool_name == "register_contact_profile":
                assert isinstance(arguments, RegisterContactProfileArguments)
                data = run_register_contact_profile_tool(
                    session, call=call, arguments=arguments, ctx=ctx
                )
            elif call.tool_name == "propose_cancellation":
                assert isinstance(arguments, ProposeCancellationArguments)
                data = run_propose_cancellation_tool(
                    session, call=call, arguments=arguments, ctx=ctx
                )
            elif call.tool_name == "confirm_cancellation":
                assert isinstance(arguments, ConfirmCancellationArguments)
                data = run_confirm_cancellation_tool(
                    session, call=call, arguments=arguments, ctx=ctx
                )
            elif call.tool_name == "propose_reschedule":
                assert isinstance(arguments, ProposeRescheduleArguments)
                data = run_propose_reschedule_tool(
                    session, call=call, arguments=arguments, ctx=ctx
                )
            elif call.tool_name == "confirm_reschedule":
                assert isinstance(arguments, ConfirmRescheduleArguments)
                data = run_confirm_reschedule_tool(
                    session, call=call, arguments=arguments, ctx=ctx
                )
            elif call.tool_name == "request_human_handoff":
                assert isinstance(arguments, HumanHandoffArguments)
                data = run_handoff_tool(
                    session, call=call, arguments=arguments, ctx=ctx
                )
            else:
                raise AssertionError(f"Unhandled mutation tool: {call.tool_name}")
        else:
            _set_statement_timeout(session)
            conversation, contact = _load_conversation_contact(
                session,
                conversation_id=call.conversation_id,
                ctx=ctx,
            )
            data = _execute_tool(
                session,
                call=call,
                arguments=arguments,
                conversation=conversation,
                contact=contact,
                ctx=ctx,
            )
    except AppError as exc:
        elapsed = _duration_ms(started_ns)
        code = exc.code.value
        _audit_tool_call(
            session,
            call=call,
            ctx=ctx,
            status="error",
            duration_ms=elapsed,
            error_code=code,
        )
        session.commit()
        # Permission denials are transport authorization failures, not a
        # domain/tool outcome. Preserve the audit row, then let the stable
        # application error handler render HTTP 403 so dormant capabilities
        # cannot be mistaken for an executable tool result.
        if code == "PERMISSION_DENIED":
            raise
        return AgentToolResult(
            tool_version=call.tool_version,
            status="error",
            data=None,
            error=AgentToolError(
                code=code,
                message=exc.message,
                retryable=False,
                details=exc.details,
            ),
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            duration_ms=elapsed,
        )

    elapsed = _duration_ms(started_ns)
    _audit_tool_call(
        session,
        call=call,
        ctx=ctx,
        status="success",
        duration_ms=elapsed,
    )
    session.commit()
    return AgentToolResult(
        tool_version=call.tool_version,
        status="success",
        data=data,
        error=None,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        duration_ms=elapsed,
    )
