"""Deterministic, contact-safe reception-agent tool gateway."""

from __future__ import annotations

from time import perf_counter_ns
from typing import TypeAlias

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.agent_tools.registry import (
    TOOL_REGISTRY,
    ToolSpec,
    allowed_tools,
    resolve_agent_key,
)
from app.agent_tools.schemas import (
    AgentToolCall,
    AgentToolCatalog,
    AgentToolDescriptor,
    AgentToolError,
    AgentToolResult,
)
from app.audit.service import record_event, record_security_event
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.service import PERMISSION_DENIED_HTTP_STATUS, IamErrorCode

ArgumentModel: TypeAlias = type[BaseModel]
ARGUMENT_MODELS: dict[str, ArgumentModel] = {
    name: spec.args_model for name, spec in TOOL_REGISTRY.items()
}

DENIED_MESSAGE = "This tool is not available to the calling agent."


def _duration_ms(started_ns: int) -> int:
    return max(0, (perf_counter_ns() - started_ns) // 1_000_000)


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


def _deny(
    session: Session,
    *,
    spec: ToolSpec,
    agent_key: str | None,
    ctx: ExecutionContext,
) -> AppError:
    """Stage the security event; the caller's error path audits and commits."""
    record_security_event(
        session,
        event_type="agent_tool_denied",
        outcome="blocked",
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        organization_id=ctx.organization_id,
        principal_id=ctx.principal_id,
        metadata={"tool_name": spec.name, "agent_key": agent_key, "level": spec.level},
    )
    return AppError(
        IamErrorCode.PERMISSION_DENIED,
        DENIED_MESSAGE,
        details={},
        http_status=PERMISSION_DENIED_HTTP_STATUS,
    )


def _authorize_agent(
    session: Session, *, spec: ToolSpec, agent_key: str | None, ctx: ExecutionContext
) -> None:
    """Server-side allowlist gate for agent principals.

    The L4 decision is pure (``principal_type`` + ``level``) and does not
    depend on any allowlist. The ``display_name`` read behind ``agent_key`` is
    closed with ``rollback`` so a mutation handler's ``session.begin()`` still
    opens the transaction whose first statement is the receipt claim.
    """
    if ctx.principal_type != "agent":
        return
    if spec.level == "L4" or spec.name not in allowed_tools(agent_key):
        raise _deny(session, spec=spec, agent_key=agent_key, ctx=ctx)
    session.rollback()


def _audit_tool_call(
    session: Session,
    *,
    call: AgentToolCall,
    ctx: ExecutionContext,
    agent_key: str | None,
    status: str,
    duration_ms: int,
    error_code: str | None = None,
) -> None:
    metadata = {
        "tool_name": call.tool_name,
        "tool_version": call.tool_version,
        "status": status,
        "duration_ms": duration_ms,
        "agent_key": agent_key,
    }
    if error_code is not None:
        metadata["error_code"] = error_code
    record_event(
        session,
        ctx=ctx,
        entity_type="agent_tool",
        entity_id=str(call.conversation_id) if call.conversation_id is not None else "none",
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

    Order: agent allowlist gate (before trace validation, so probing is always
    recorded) -> trace -> conversation requirement -> arguments -> handler.
    Mutation handlers open their own transaction whose first statement is the
    receipt claim.
    """
    started_ns = perf_counter_ns()
    spec = TOOL_REGISTRY[call.tool_name]
    agent_key = resolve_agent_key(session, ctx)
    try:
        _authorize_agent(session, spec=spec, agent_key=agent_key, ctx=ctx)
        _validate_trace(call, ctx)
        if spec.needs_conversation and call.conversation_id is None:
            raise AppError(ErrorCode.INVALID_INPUT, "This tool requires a conversation_id.")
        arguments = _parse_arguments(call)
        data = spec.handler(session, call=call, arguments=arguments, ctx=ctx)
    except AppError as exc:
        elapsed = _duration_ms(started_ns)
        code = exc.code.value
        _audit_tool_call(
            session,
            call=call,
            ctx=ctx,
            agent_key=agent_key,
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
        agent_key=agent_key,
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


def list_agent_tools(session: Session, *, ctx: ExecutionContext) -> AgentToolCatalog:
    """The tools the caller may invoke. Agents see their allowlist (never L4);
    other principals see the full registry, since permissions still apply on
    ``/agent-tools/call``. Read-only."""
    visible = allowed_tools(resolve_agent_key(session, ctx))
    return AgentToolCatalog(
        tools=[
            AgentToolDescriptor(
                name=spec.name,
                tool_version="1.0" if spec.effect == "read" else "1.1",
                effect=spec.effect,
                level=spec.level,
                needs_conversation=spec.needs_conversation,
                description=spec.description,
                arguments_schema=spec.args_model.model_json_schema(),
            )
            for name, spec in TOOL_REGISTRY.items()
            if name in visible
        ]
    )
