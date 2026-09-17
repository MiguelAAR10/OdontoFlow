"""Bounded LangChain Sales Agent runtime.

The module imports optional LangChain/LangGraph dependencies lazily. This keeps
the canonical ``app`` process and its base dependency set independent from the
agent process.
"""

from __future__ import annotations

import json
import logging
import math
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


class SalesAgentProviderTimeout(SalesAgentExecutionError):
    """The configured model provider exceeded one bounded request."""


class SalesAgentTurnTimeout(SalesAgentExecutionError):
    """The complete Sales Agent turn exceeded its configured deadline."""


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
        try:
            result = handler(request)
            ensure_deadline()
            if _tool_result_failed(result):
                telemetry.tool_failures += 1
            return result
        except SalesAgentTurnTimeout:
            raise
        except Exception:
            telemetry.tool_failures += 1
            tool_call = getattr(request, "tool_call", {})
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
        try:
            self._ensure_turn_deadline(deadline)
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
            agent = self._build_agent(
                request.conversation_id,
                telemetry,
                deadline=deadline,
            )
            self.agent = agent
            self._ensure_turn_deadline(deadline)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": inbound.text}]},
                config=self.invoke_config(request.conversation_id),
            )
            self._ensure_turn_deadline(deadline)
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
        except SalesAgentTurnTimeout:
            outcome = "timeout"
            raise
        except SalesAgentProviderTimeout:
            outcome = "provider_timeout"
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
                raise SalesAgentProviderTimeout(
                    "The model provider request exceeded its configured timeout."
                ) from exc
            if isinstance(exc, (GatewayError, AgentUnavailableError, ValueError)):
                raise
            raise SalesAgentExecutionError(
                "The Sales Agent could not complete this turn safely."
            ) from exc
        finally:
            latency_ms = max(0, (perf_counter_ns() - started_ns) // 1_000_000)
            self.telemetry_sink(telemetry.as_dict(latency_ms=latency_ms, outcome=outcome))

    @staticmethod
    def _emit_telemetry(event: dict[str, Any]) -> None:
        logger.info(json.dumps(event, sort_keys=True, separators=(",", ":")))


__all__ = [
    "SalesAgentExecutionError",
    "SalesAgentProviderTimeout",
    "SalesAgentRuntime",
    "SalesAgentTurnTimeout",
    "SalesAgentTurnRequest",
]
