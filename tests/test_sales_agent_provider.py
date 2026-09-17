"""Configuration and provider-boundary proofs for the Sales Agent runtime."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from sales_agent.config import (
    DEFAULT_MODEL,
    OPENROUTER_BASE_URL,
    AgentSettings,
)
from sales_agent.schemas import AgentUnavailableError


def _clear_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SALES_AGENT_MODEL_PROVIDER",
        "SALES_AGENT_MODEL",
        "SALES_AGENT_MODEL_BASE_URL",
        "SALES_AGENT_MODEL_TIMEOUT_SECONDS",
        "SALES_AGENT_TURN_TIMEOUT_SECONDS",
        "SALES_AGENT_MODEL_MAX_OUTPUT_TOKENS",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**changes) -> AgentSettings:
    settings = AgentSettings(
        backend_base_url="http://backend.test",
        backend_credential="service-token",
        agent_database_url=(
            "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/agent_test"
        ),
        model=DEFAULT_MODEL,
        recursion_limit=12,
        request_timeout_seconds=1.0,
        model_provider="openrouter",
        model_base_url=OPENROUTER_BASE_URL,
        model_api_key="router-test-key",
    )
    return replace(settings, **changes)


def test_openrouter_is_the_default_sandbox_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_environment(monkeypatch)

    settings = AgentSettings.from_env()

    assert settings.model_provider == "openrouter"
    assert settings.model == "deepseek/deepseek-v4-flash-0731"
    assert settings.model_base_url == OPENROUTER_BASE_URL
    assert settings.model_api_key is None


def test_openrouter_reads_its_own_key_without_falling_back_to_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "native-openai-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")

    settings = AgentSettings.from_env()

    assert settings.model_api_key == "openrouter-test-key"
    assert settings.model_api_key != "native-openai-test-key"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert AgentSettings.from_env().model_api_key is None


def test_native_openai_configuration_remains_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("SALES_AGENT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("SALES_AGENT_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "native-openai-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")

    settings = AgentSettings.from_env()

    assert settings.model_provider == "openai"
    assert settings.model == "gpt-5.4-mini"
    assert settings.model_base_url is None
    assert settings.model_api_key == "native-openai-test-key"


def test_runtime_uses_openai_compatibility_with_openrouter_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_init_chat_model(model: str, **kwargs):
        observed["model"] = model
        observed.update(kwargs)
        return object()

    import langchain.chat_models

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init_chat_model)

    from sales_agent.runtime import SalesAgentRuntime

    runtime = SalesAgentRuntime(gateway=object(), settings=_settings())
    resolved = runtime._resolve_model()

    assert resolved is runtime._model
    assert observed == {
        "model": DEFAULT_MODEL,
        "model_provider": "openai",
        "api_key": "router-test-key",
        "base_url": OPENROUTER_BASE_URL,
        "timeout": 20.0,
        "max_retries": 0,
        "max_tokens": 512,
        "use_responses_api": False,
    }


def test_runtime_passes_explicit_bounded_provider_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_init_chat_model(model: str, **kwargs):
        observed["model"] = model
        observed.update(kwargs)
        return object()

    import langchain.chat_models

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init_chat_model)

    from sales_agent.runtime import SalesAgentRuntime

    runtime = SalesAgentRuntime(
        gateway=object(),
        settings=_settings(
            model_timeout_seconds=7.5,
            turn_timeout_seconds=45.0,
            model_max_output_tokens=256,
        ),
    )
    runtime._resolve_model()

    assert observed["timeout"] == 7.5
    assert observed["max_retries"] == 0
    assert observed["max_tokens"] == 256
    assert runtime.settings.turn_timeout_seconds == 45.0


def test_native_openai_runtime_keeps_provider_boundary_and_bounded_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_init_chat_model(model: str, **kwargs):
        observed["model"] = model
        observed.update(kwargs)
        return object()

    import langchain.chat_models

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init_chat_model)

    from sales_agent.runtime import SalesAgentRuntime

    runtime = SalesAgentRuntime(
        gateway=object(),
        settings=_settings(
            model="gpt-5.4-mini",
            model_provider="openai",
            model_base_url=None,
            model_api_key="native-openai-test-key",
        ),
    )
    runtime._resolve_model()

    assert observed == {
        "model": "gpt-5.4-mini",
        "model_provider": "openai",
        "api_key": "native-openai-test-key",
        "timeout": 20.0,
        "max_retries": 0,
        "max_tokens": 512,
    }


def test_bounded_runtime_settings_are_explicitly_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_environment(monkeypatch)
    monkeypatch.setenv("SALES_AGENT_MODEL_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("SALES_AGENT_TURN_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("SALES_AGENT_MODEL_MAX_OUTPUT_TOKENS", "256")

    settings = AgentSettings.from_env()

    assert (
        settings.model_timeout_seconds,
        settings.turn_timeout_seconds,
        settings.model_max_output_tokens,
    ) == (7.5, 45.0, 256)


def test_turn_deadline_is_classified_without_retrying_or_fabricating_a_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sales_agent.runtime import SalesAgentRuntime, SalesAgentTurnTimeout
    from sales_agent.schemas import SalesAgentTurnRequest

    events: list[dict] = []
    runtime = SalesAgentRuntime(
        gateway=object(),
        settings=_settings(turn_timeout_seconds=1.0),
        telemetry_sink=events.append,
    )

    def expired(_deadline: float) -> None:
        raise SalesAgentTurnTimeout("turn deadline exceeded")

    monkeypatch.setattr(runtime, "_ensure_turn_deadline", expired)

    with pytest.raises(SalesAgentTurnTimeout):
        runtime.turn(
            SalesAgentTurnRequest(conversation_id=42, latest_inbound_message_id=7)
        )

    assert events[-1]["outcome"] == "timeout"


def test_provider_timeout_is_classified_at_the_runtime_boundary() -> None:
    from sales_agent.runtime import SalesAgentProviderTimeout, SalesAgentRuntime
    from sales_agent.schemas import SalesAgentTurnRequest

    class Gateway:
        def load_latest_inbound_message(self, conversation_id, message_id):
            return {"id": message_id, "text": "Synthetic inbound"}

    class TimedOutAgent:
        def invoke(self, *_args, **_kwargs):
            raise TimeoutError("provider deadline")

    events: list[dict] = []
    runtime = SalesAgentRuntime(
        gateway=Gateway(),
        settings=_settings(),
        telemetry_sink=events.append,
    )
    runtime._build_agent = lambda *_args, **_kwargs: TimedOutAgent()

    with pytest.raises(SalesAgentProviderTimeout) as failure:
        runtime.turn(
            SalesAgentTurnRequest(conversation_id=42, latest_inbound_message_id=7)
        )

    assert events[-1]["outcome"] == "provider_timeout"
    assert failure.value.diagnostic.category == "provider_timeout"
    assert failure.value.diagnostic.stage == "model_execution"


def test_provider_status_failure_is_classified_without_serializing_provider_body() -> None:
    import httpx
    import openai

    from sales_agent.runtime import diagnostic_for_exception

    request = httpx.Request("POST", "https://router.invalid")
    response = httpx.Response(
        401,
        headers={"x-request-id": "req_synthetic_123"},
        request=request,
    )
    failure = openai.AuthenticationError(
        "SYNTHETIC_PROVIDER_BODY",
        response=response,
        body={"detail": "SYNTHETIC_PROVIDER_BODY"},
    )

    diagnostic = diagnostic_for_exception(
        failure,
        stage="model_execution",
        partial_business_effects=False,
    )
    encoded = json.dumps(diagnostic.as_dict(elapsed_ms=7500))

    assert diagnostic.as_dict(elapsed_ms=7500) == {
        "stage": "model_execution",
        "category": "provider_authentication",
        "upstream_status": 401,
        "upstream_request_id": "req_synthetic_123",
        "elapsed_ms": 7500,
        "partial_business_effects": False,
    }
    assert "SYNTHETIC_PROVIDER_BODY" not in encoded


def test_unknown_runtime_failure_remains_explicitly_unknown() -> None:
    from sales_agent.runtime import diagnostic_for_exception

    diagnostic = diagnostic_for_exception(
        RuntimeError("SYNTHETIC_UNKNOWN_FAILURE"),
        stage="model_execution",
    )

    assert diagnostic.as_dict() == {
        "stage": "model_execution",
        "category": "unknown",
    }


def test_runtime_wraps_unknown_failure_with_safe_diagnostic_and_no_tool_retry() -> None:
    from sales_agent.runtime import SalesAgentExecutionError, SalesAgentRuntime
    from sales_agent.schemas import SalesAgentTurnRequest

    class Gateway:
        def __init__(self):
            self.tool_calls: list[str] = []

        def load_latest_inbound_message(self, conversation_id, message_id):
            return {"id": message_id, "text": "Synthetic inbound"}

        def call_tool(self, tool_name, *, conversation_id, arguments):
            self.tool_calls.append(tool_name)
            raise AssertionError("a failed model turn must not retry a tool")

    class FailingAgent:
        def invoke(self, *_args, **_kwargs):
            raise RuntimeError("SYNTHETIC_RUNTIME_FAILURE")

    gateway = Gateway()
    runtime = SalesAgentRuntime(gateway=gateway, settings=_settings())
    runtime._build_agent = lambda *_args, **_kwargs: FailingAgent()

    with pytest.raises(SalesAgentExecutionError) as failure:
        runtime.turn(
            SalesAgentTurnRequest(conversation_id=42, latest_inbound_message_id=7)
        )

    assert failure.value.diagnostic.as_dict() == {
        "stage": "model_execution",
        "category": "unknown",
        "partial_business_effects": False,
    }
    assert gateway.tool_calls == []


def test_missing_openrouter_key_fails_closed_before_provider_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_provider_execution(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider initialization must not run without a key")

    import langchain.chat_models

    monkeypatch.setattr(
        langchain.chat_models,
        "init_chat_model",
        unexpected_provider_execution,
    )

    from sales_agent.runtime import SalesAgentRuntime

    runtime = SalesAgentRuntime(
        gateway=object(),
        settings=_settings(model_api_key=None),
    )

    with pytest.raises(AgentUnavailableError, match="OPENROUTER_API_KEY"):
        runtime._resolve_model()

    assert called is False


def test_model_provider_credentials_and_base_url_are_not_runtime_configurable() -> None:
    from sales_agent.runtime import SalesAgentRuntime

    runtime = SalesAgentRuntime(gateway=object(), settings=_settings())
    invocation_config = runtime.invoke_config(42)

    assert SalesAgentRuntime.invoke_config_fields() == {
        "configurable",
        "thread_id",
        "recursion_limit",
    }
    assert set(invocation_config) == {"configurable", "recursion_limit"}
    assert set(invocation_config["configurable"]) == {"thread_id"}
    for forbidden in {
        "model",
        "model_provider",
        "base_url",
        "api_key",
        "credentials",
    }:
        assert forbidden not in invocation_config
        assert forbidden not in invocation_config["configurable"]
