"""Configuration and provider-boundary proofs for the Sales Agent runtime."""

from __future__ import annotations

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
        "use_responses_api": False,
    }


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
