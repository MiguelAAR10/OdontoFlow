"""Bounded LangChain Sales Agent runtime.

The module imports optional LangChain/LangGraph dependencies lazily. This keeps
the canonical ``app`` process and its base dependency set independent from the
agent process.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from time import monotonic, perf_counter_ns
from typing import TYPE_CHECKING, Any, Callable

from sales_agent.config import (
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_PROVIDER,
    AgentSettings,
    get_settings,
)
from sales_agent.schemas import (
    MUTATION_TOOL_NAMES,
    AgentUnavailableError,
    GatewayError,
    InboundMessage,
    SalesAgentResponse,
    SalesAgentTurnRequest,
    SalesAgentTurnResponse,
)

if TYPE_CHECKING:
    from sales_agent.gateway import BackendGateway

logger = logging.getLogger("sales_agent.telemetry")

SYSTEM_PROMPT = """You are the OdontoFlow Sales Agent.

Use only the provided typed tools. Canonical services determine service
duration, availability, prices (which are not exposed in V0), and bookings.
Never invent a slot, price, promotion, practitioner, diagnosis, prescription,
or clinical answer. Ask for explicit confirmation of an exact pending proposal
before confirming it. If the contact asks for a person, reports urgency, asks
for a clinical answer or pricing exception, or you cannot proceed confidently,
call request_human_handoff. Finish with the required structured response.
"""


@dataclass
class _TurnTelemetry:
    conversation_id: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    mutating_tool_calls: int = 0
    successful_mutating_tool_calls: int = 0

    def as_dict(self, *, latency_ms: int, outcome: str) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "latency_ms": latency_ms,
            "outcome": outcome,
        }


class SalesAgentExecutionError(RuntimeError):
    """A safe, non-content-bearing agent execution failure."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: "SalesAgentDiagnostic | None" = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class SalesAgentProviderTimeout(SalesAgentExecutionError):
    """The configured model provider exceeded one bounded request."""


class SalesAgentTurnTimeout(SalesAgentExecutionError):
    """The complete Sales Agent turn exceeded its configured deadline."""


_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIAGNOSTIC_STAGES = frozenset(
    {
        "turn_deadline",
        "inbound_context",
        "agent_build",
        "model_execution",
        "response_validation",
        "request_boundary",
        "gateway",
    }
)
_DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "provider_authentication",
        "provider_rate_limited",
        "provider_invalid_request",
        "provider_model_unavailable",
        "provider_server_error",
        "provider_http_error",
        "provider_connection",
        "provider_timeout",
        "provider_invalid_response",
        "turn_timeout",
        "gateway",
        "runtime_unavailable",
        "invalid_agent_response",
        "unknown",
    }
)


@dataclass(frozen=True)
class SalesAgentDiagnostic:
    """Safe, content-free evidence for one failed Sales Agent turn."""

    stage: str
    category: str
    upstream_status: int | None = None
    upstream_request_id: str | None = None
    elapsed_ms: int | None = None
    partial_business_effects: bool | None = None

    def as_dict(self, *, elapsed_ms: int | None = None) -> dict[str, Any]:
        """Return only allowlisted, bounded diagnostic fields."""
        payload: dict[str, Any] = {
            "stage": (
                self.stage
                if isinstance(self.stage, str) and self.stage in _DIAGNOSTIC_STAGES
                else "request_boundary"
            ),
            "category": (
                self.category
                if isinstance(self.category, str) and self.category in _DIAGNOSTIC_CATEGORIES
                else "unknown"
            ),
        }
        status = _safe_status_code(self.upstream_status)
        request_id = _safe_request_id_value(self.upstream_request_id)
        if status is not None:
            payload["upstream_status"] = status
        if request_id is not None:
            payload["upstream_request_id"] = request_id
        measured_elapsed = elapsed_ms if elapsed_ms is not None else self.elapsed_ms
        if isinstance(measured_elapsed, int) and not isinstance(measured_elapsed, bool):
            payload["elapsed_ms"] = max(0, measured_elapsed)
        if isinstance(self.partial_business_effects, bool):
            payload["partial_business_effects"] = self.partial_business_effects
        return payload


def _safe_status_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _safe_request_id_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_REQUEST_ID.fullmatch(candidate) else None


def _is_openai_exception(exc: BaseException, module: Any, name: str) -> bool:
    exception_type = getattr(module, name, None)
    return isinstance(exception_type, type) and isinstance(exc, exception_type)


def _partial_business_effects(telemetry: _TurnTelemetry) -> bool | None:
    if telemetry.successful_mutating_tool_calls > 0:
        return True
    if telemetry.mutating_tool_calls > 0:
        return None
    return False


def diagnostic_for_exception(
    exc: BaseException,
    *,
    stage: str,
    partial_business_effects: bool | None = None,
) -> SalesAgentDiagnostic:
    """Classify known SDK/runtime failures without retaining exception text."""
    existing = getattr(exc, "diagnostic", None)
    if isinstance(existing, SalesAgentDiagnostic):
        return existing

    source = exc
    if isinstance(exc, SalesAgentExecutionError) and exc.__cause__ is not None:
        source = exc.__cause__

    if isinstance(source, SalesAgentTurnTimeout) or isinstance(exc, SalesAgentTurnTimeout):
        return SalesAgentDiagnostic(
            stage=stage,
            category="turn_timeout",
            partial_business_effects=partial_business_effects,
        )
    if isinstance(source, GatewayError):
        return SalesAgentDiagnostic(
            stage=stage,
            category="gateway",
            upstream_status=_safe_status_code(source.status_code),
            partial_business_effects=partial_business_effects,
        )
    if isinstance(source, AgentUnavailableError):
        return SalesAgentDiagnostic(
            stage=stage,
            category="runtime_unavailable",
            partial_business_effects=partial_business_effects,
        )
    if isinstance(source, ValueError):
        category = (
            "invalid_agent_response"
            if stage == "response_validation"
            else "unknown"
        )
        return SalesAgentDiagnostic(
            stage=stage,
            category=category,
            partial_business_effects=partial_business_effects,
        )
    if _is_timeout_exception(source) or isinstance(exc, SalesAgentProviderTimeout):
        return SalesAgentDiagnostic(
            stage=stage,
            category="provider_timeout",
            partial_business_effects=partial_business_effects,
        )

    try:
        import openai
    except ImportError:  # pragma: no cover - openai is part of langchain-openai
        openai = None

    if openai is not None:
        request_id = _safe_request_id_value(getattr(source, "request_id", None))
        status = _safe_status_code(getattr(source, "status_code", None))
        if _is_openai_exception(source, openai, "AuthenticationError"):
            category = "provider_authentication"
        elif _is_openai_exception(source, openai, "PermissionDeniedError"):
            category = "provider_authentication"
        elif _is_openai_exception(source, openai, "RateLimitError"):
            category = "provider_rate_limited"
        elif _is_openai_exception(source, openai, "BadRequestError"):
            category = "provider_invalid_request"
        elif _is_openai_exception(source, openai, "UnprocessableEntityError"):
            category = "provider_invalid_request"
        elif _is_openai_exception(source, openai, "NotFoundError"):
            category = "provider_model_unavailable"
        elif _is_openai_exception(source, openai, "InternalServerError"):
            category = "provider_server_error"
        elif _is_openai_exception(source, openai, "APIResponseValidationError"):
            category = "provider_invalid_response"
        elif _is_openai_exception(source, openai, "APIConnectionError"):
            category = "provider_connection"
        elif _is_openai_exception(source, openai, "APIStatusError"):
            category = {
                400: "provider_invalid_request",
                401: "provider_authentication",
                403: "provider_authentication",
                404: "provider_model_unavailable",
                422: "provider_invalid_request",
                429: "provider_rate_limited",
            }.get(status, "provider_server_error" if status and status >= 500 else "provider_http_error")
        else:
            category = None
        if category is not None:
            return SalesAgentDiagnostic(
                stage=stage,
                category=category,
                upstream_status=status,
                upstream_request_id=request_id,
                partial_business_effects=partial_business_effects,
            )

    return SalesAgentDiagnostic(
        stage=stage,
        category="unknown",
        partial_business_effects=partial_business_effects,
    )


def _is_timeout_exception(exc: BaseException) -> bool:
    """Recognize provider/client timeout types without importing optional code eagerly."""
    if isinstance(exc, TimeoutError):
        return True
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return True
    except ImportError:  # pragma: no cover - httpx is part of the agent extra
        pass
    try:
        from openai import APITimeoutError

        return isinstance(exc, APITimeoutError)
    except ImportError:  # pragma: no cover - openai is part of langchain-openai
        return False


def _usage_value(usage: Any, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _record_model_usage(response: Any, telemetry: _TurnTelemetry) -> None:
    """Extract provider/fake usage metadata without logging model content."""
    messages = getattr(response, "result", None)
    if messages is None:
        messages = [response]
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            telemetry.input_tokens += _usage_value(usage, "input_tokens", "prompt_tokens")
            telemetry.output_tokens += _usage_value(
                usage, "output_tokens", "completion_tokens"
            )
            continue
        metadata = getattr(message, "response_metadata", None)
        if not isinstance(metadata, dict):
            continue
        usage = metadata.get("token_usage") or metadata.get("usage") or metadata
        telemetry.input_tokens += _usage_value(usage, "input_tokens", "prompt_tokens")
        telemetry.output_tokens += _usage_value(
            usage, "output_tokens", "completion_tokens"
        )


def _tool_result_failed(result: Any) -> bool:
    """Detect a typed ``status:error`` envelope without recording its contents."""
    items = result if isinstance(result, (list, tuple)) else [result]
    for item in items:
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if isinstance(content, dict) and content.get("status") == "error":
            return True
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict) and decoded.get("status") == "error":
                return True
    return False


def _build_middleware(
    telemetry: _TurnTelemetry,
    *,
    deadline: float | None = None,
):
    """Create current-compatible tool/model middleware for one turn."""
    try:
        from langchain.agents.middleware import AgentMiddleware, wrap_tool_call
        from langchain.messages import ToolMessage
    except ImportError as exc:  # pragma: no cover - exercised in base env
        raise AgentUnavailableError(
            "Install the sales-agent optional dependency group to run the agent."
        ) from exc

    def ensure_deadline() -> None:
        if deadline is not None and monotonic() >= deadline:
            raise SalesAgentTurnTimeout("The Sales Agent turn deadline was exceeded.")

    @wrap_tool_call
    def telemetry_tool_call(request, handler):
        ensure_deadline()
        telemetry.tool_calls += 1
        tool_call = getattr(request, "tool_call", {})
        tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
        is_mutating = tool_name in MUTATION_TOOL_NAMES
        if is_mutating:
            telemetry.mutating_tool_calls += 1
        try:
            result = handler(request)
            if _tool_result_failed(result):
                telemetry.tool_failures += 1
            elif is_mutating:
                telemetry.successful_mutating_tool_calls += 1
            ensure_deadline()
            return result
        except SalesAgentTurnTimeout:
            raise
        except Exception:
            telemetry.tool_failures += 1
            tool_call_id = tool_call.get("id", "unknown") if isinstance(tool_call, dict) else "unknown"
            return ToolMessage(
                content="The backend tool failed safely. Request human reception if needed.",
                tool_call_id=tool_call_id,
            )

    class ModelTelemetryMiddleware(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            ensure_deadline()
            telemetry.model_calls += 1
            try:
                response = handler(request)
            except SalesAgentTurnTimeout:
                raise
            except Exception as exc:
                if _is_timeout_exception(exc):
                    raise SalesAgentProviderTimeout(
                        "The model provider request exceeded its configured timeout."
                    ) from exc
                raise
            ensure_deadline()
            _record_model_usage(response, telemetry)
            return response

    return [telemetry_tool_call, ModelTelemetryMiddleware()]


class SalesAgentRuntime:
    """Run one bounded, conversation-threaded LangChain agent turn."""

    def __init__(
        self,
        *,
        gateway: BackendGateway,
        model: Any | None = None,
        checkpointer: Any | None = None,
        settings: AgentSettings | None = None,
        recursion_limit: int | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway
        self.model_name = self.settings.model
        self.recursion_limit = (
            self.settings.recursion_limit
            if recursion_limit is None
            else recursion_limit
        )
        if self.recursion_limit <= 0 or self.recursion_limit > 100:
            raise ValueError("recursion_limit must be between 1 and 100.")
        if (
            not math.isfinite(self.settings.model_timeout_seconds)
            or self.settings.model_timeout_seconds <= 0
        ):
            raise ValueError("model_timeout_seconds must be greater than zero.")
        if (
            not math.isfinite(self.settings.turn_timeout_seconds)
            or self.settings.turn_timeout_seconds <= 0
        ):
            raise ValueError("turn_timeout_seconds must be greater than zero.")
        if self.settings.model_max_output_tokens <= 0:
            raise ValueError("model_max_output_tokens must be greater than zero.")
        self.checkpointer = checkpointer
        self.telemetry_sink = telemetry_sink or self._emit_telemetry
        self._model = model
        self.agent: Any | None = None

    @staticmethod
    def invoke_config_fields() -> set[str]:
        return {"configurable", "thread_id", "recursion_limit"}

    @staticmethod
    def structured_response_fields() -> set[str]:
        return set(SalesAgentResponse.model_fields)

    def invoke_config(self, conversation_id: int) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": str(conversation_id)},
            "recursion_limit": self.recursion_limit,
        }

    def _resolve_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.settings.model_api_key:
            key_name = (
                "OPENROUTER_API_KEY"
                if self.settings.model_provider == "openrouter"
                else "OPENAI_API_KEY"
            )
            raise AgentUnavailableError(
                f"The configured model provider credential is missing: {key_name}."
            )
        if self.settings.model_provider == "openrouter":
            if self.settings.model_base_url != OPENROUTER_BASE_URL:
                raise AgentUnavailableError(
                    "The OpenRouter model base URL is not configured correctly."
                )
            integration_provider = OPENROUTER_MODEL_PROVIDER
        elif self.settings.model_provider == "openai":
            integration_provider = "openai"
        else:  # pragma: no cover - AgentSettings validates configured providers
            raise AgentUnavailableError("The configured model provider is unsupported.")
        try:
            from langchain.chat_models import init_chat_model
        except ImportError as exc:  # pragma: no cover - exercised in base env
            raise AgentUnavailableError(
                "Install the sales-agent optional dependency group to run the agent."
            ) from exc
        kwargs: dict[str, Any] = {
            "model_provider": integration_provider,
            "api_key": self.settings.model_api_key,
            "timeout": self.settings.model_timeout_seconds,
            "max_retries": 0,
            "max_tokens": self.settings.model_max_output_tokens,
        }
        if self.settings.model_base_url:
            kwargs["base_url"] = self.settings.model_base_url
        if self.settings.model_provider == "openrouter":
            # Keep the OpenAI-compatible gateway on Chat Completions, the
            # stable tool-calling surface supported by OpenRouter models.
            kwargs["use_responses_api"] = False
        self._model = init_chat_model(self.settings.model, **kwargs)
        return self._model

    def _build_agent(
        self,
        conversation_id: int,
        telemetry: _TurnTelemetry,
        *,
        deadline: float | None = None,
    ):
        try:
            from langchain.agents import create_agent
            from langchain.agents.structured_output import ToolStrategy
        except ImportError as exc:  # pragma: no cover - exercised in base env
            raise AgentUnavailableError(
                "Install the sales-agent optional dependency group to run the agent."
            ) from exc
        from sales_agent.tools import build_v0_tools

        tools = build_v0_tools(self.gateway, conversation_id=conversation_id)
        agent = create_agent(
            model=self._resolve_model(),
            tools=list(tools),
            system_prompt=SYSTEM_PROMPT,
            middleware=_build_middleware(telemetry, deadline=deadline),
            response_format=ToolStrategy(SalesAgentResponse),
            checkpointer=self.checkpointer,
        )
        return agent

    @staticmethod
    def _ensure_turn_deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise SalesAgentTurnTimeout("The Sales Agent turn deadline was exceeded.")

    def _request_handoff(
        self,
        *,
        conversation_id: int,
        telemetry: _TurnTelemetry,
        reason_summary: str,
    ) -> None:
        """Use the typed handoff wrapper when the bounded loop is exhausted."""
        try:
            from sales_agent.tools import build_v0_tools

            handoff_tool = build_v0_tools(
                self.gateway, conversation_id=conversation_id
            )[-1]
            telemetry.tool_calls += 1
            result = handoff_tool.invoke(
                {
                    "reason_code": "low_confidence",
                    "reason_summary": reason_summary,
                }
            )
            if not isinstance(result, dict) or result.get("status") != "success":
                telemetry.tool_failures += 1
        except Exception:
            telemetry.tool_failures += 1

    def turn(self, request: SalesAgentTurnRequest) -> SalesAgentTurnResponse:
        started_ns = perf_counter_ns()
        deadline = monotonic() + self.settings.turn_timeout_seconds
        telemetry = _TurnTelemetry(
            conversation_id=request.conversation_id,
            model=self.model_name,
        )
        outcome = "error"
        stage = "turn_deadline"
        try:
            self._ensure_turn_deadline(deadline)
            stage = "inbound_context"
            try:
                inbound = self.gateway.load_latest_inbound_message(
                    request.conversation_id,
                    request.latest_inbound_message_id,
                )
            except GatewayError:
                telemetry.tool_failures += 1
                raise
            if isinstance(inbound, dict):
                inbound = InboundMessage.model_validate(inbound)
            self._ensure_turn_deadline(deadline)
            stage = "agent_build"
            agent = self._build_agent(
                request.conversation_id,
                telemetry,
                deadline=deadline,
            )
            self.agent = agent
            self._ensure_turn_deadline(deadline)
            stage = "model_execution"
            result = agent.invoke(
                {"messages": [{"role": "user", "content": inbound.text}]},
                config=self.invoke_config(request.conversation_id),
            )
            self._ensure_turn_deadline(deadline)
            stage = "response_validation"
            structured = result.get("structured_response") if isinstance(result, dict) else None
            if isinstance(structured, SalesAgentResponse):
                response = structured
            else:
                response = SalesAgentResponse.model_validate(structured)
            outcome = response.outcome
            return SalesAgentTurnResponse(
                conversation_id=request.conversation_id,
                latest_inbound_message_id=request.latest_inbound_message_id,
                reply=response.reply,
                outcome=response.outcome,
                handoff=response.handoff,
            )
        except SalesAgentTurnTimeout as exc:
            outcome = "timeout"
            exc.diagnostic = diagnostic_for_exception(
                exc,
                stage=stage,
                partial_business_effects=_partial_business_effects(telemetry),
            )
            raise
        except SalesAgentProviderTimeout as exc:
            outcome = "provider_timeout"
            exc.diagnostic = diagnostic_for_exception(
                exc,
                stage=stage,
                partial_business_effects=_partial_business_effects(telemetry),
            )
            raise
        except Exception as exc:
            try:
                from langgraph.errors import GraphRecursionError
            except ImportError:
                GraphRecursionError = ()
            if GraphRecursionError and isinstance(exc, GraphRecursionError):
                try:
                    self._ensure_turn_deadline(deadline)
                except SalesAgentTurnTimeout:
                    outcome = "timeout"
                    raise
                self._request_handoff(
                    conversation_id=request.conversation_id,
                    telemetry=telemetry,
                    reason_summary="The Sales Agent reached its safe execution bound.",
                )
                outcome = "handoff"
                return SalesAgentTurnResponse(
                    conversation_id=request.conversation_id,
                    latest_inbound_message_id=request.latest_inbound_message_id,
                    reply="I’m transferring this conversation to human reception.",
                    outcome="handoff",
                    handoff=True,
                )
            if _is_timeout_exception(exc):
                outcome = "provider_timeout"
                diagnostic = diagnostic_for_exception(
                    exc,
                    stage=stage,
                    partial_business_effects=_partial_business_effects(telemetry),
                )
                raise SalesAgentProviderTimeout(
                    "The model provider request exceeded its configured timeout.",
                    diagnostic=diagnostic,
                ) from exc
            diagnostic = diagnostic_for_exception(
                exc,
                stage=stage,
                partial_business_effects=_partial_business_effects(telemetry),
            )
            if isinstance(exc, (GatewayError, AgentUnavailableError, ValueError)):
                setattr(exc, "diagnostic", diagnostic)
                raise
            raise SalesAgentExecutionError(
                "The Sales Agent could not complete this turn safely.",
                diagnostic=diagnostic,
            ) from exc
        finally:
            latency_ms = max(0, (perf_counter_ns() - started_ns) // 1_000_000)
            self.telemetry_sink(telemetry.as_dict(latency_ms=latency_ms, outcome=outcome))

    @staticmethod
    def _emit_telemetry(event: dict[str, Any]) -> None:
        logger.info(json.dumps(event, sort_keys=True, separators=(",", ":")))


__all__ = [
    "SalesAgentDiagnostic",
    "SalesAgentExecutionError",
    "SalesAgentProviderTimeout",
    "SalesAgentRuntime",
    "SalesAgentTurnTimeout",
    "SalesAgentTurnRequest",
    "diagnostic_for_exception",
]
