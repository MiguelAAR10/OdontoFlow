"""Credential-free local Sales Agent preflight proofs."""

from __future__ import annotations

from scripts.preflight_sales_agent import collect_preflight, render_preflight


def _configured_environment() -> dict[str, str]:
    return {
        "SALES_AGENT_MODEL_PROVIDER": "openrouter",
        "SALES_AGENT_MODEL": "deepseek/deepseek-v4-flash-0731",
        "SALES_AGENT_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY": "",
        "SALES_AGENT_V0_CREDENTIAL": "agent-token-for-preflight-test",
        "DATABASE_URL": "postgresql+psycopg://local/odontoflow",
        "SALES_AGENT_DATABASE_URL": "postgresql+psycopg://local/odontoflow_agent",
        "SANDBOX_BACKEND_URL": "http://127.0.0.1:8000",
        "SANDBOX_RECEIVER_URL": "http://127.0.0.1:8000/internal/sandbox/receive",
        "SANDBOX_DISPATCHER_TOKEN": "dispatcher-token-for-preflight-test",
        "SANDBOX_INBOUND_TOKEN": "inbound-token-for-preflight-test",
    }


def test_preflight_reports_not_ready_without_openrouter_key() -> None:
    result = collect_preflight(_configured_environment())

    assert result == {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash-0731",
        "base_url_configured": "true",
        "openrouter_api_key_configured": "false",
        "sales_agent_credential_configured": "true",
        "postgres_configured": "true",
        "sandbox_configured": "true",
        "ready_for_real_model_smoke": "false",
    }


def test_preflight_becomes_ready_when_only_openrouter_key_is_added() -> None:
    environment = _configured_environment()
    environment["OPENROUTER_API_KEY"] = "openrouter-test-key"

    result = collect_preflight(environment)

    assert result["openrouter_api_key_configured"] == "true"
    assert result["ready_for_real_model_smoke"] == "true"


def test_preflight_output_is_sanitized() -> None:
    environment = _configured_environment()
    environment["OPENROUTER_API_KEY"] = "do-not-print-this-key"

    rendered = render_preflight(collect_preflight(environment))

    assert "do-not-print-this-key" not in rendered
    assert "agent-token-for-preflight-test" not in rendered
    assert "dispatcher-token-for-preflight-test" not in rendered
    assert "provider=openrouter" in rendered
