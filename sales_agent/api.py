"""FastAPI entrypoint for the optional Sales Agent process."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sales_agent.config import AgentSettings, get_settings
from sales_agent.schemas import (
    AgentUnavailableError,
    GatewayError,
    SalesAgentTurnRequest,
    SalesAgentTurnResponse,
)


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
    )


def _build_runtime(settings: AgentSettings):
    try:
        from sales_agent.gateway import BackendGateway
        from sales_agent.memory import PostgresAgentMemory
        from sales_agent.runtime import SalesAgentRuntime
    except ImportError as exc:  # pragma: no cover - exercised in base env
        raise AgentUnavailableError(
            "Install the sales-agent optional dependency group to run the agent."
        ) from exc
    memory = PostgresAgentMemory.open(settings.agent_database_url)
    gateway = BackendGateway(
        settings.backend_base_url,
        settings.backend_credential,
        timeout=settings.request_timeout_seconds,
    )
    runtime = SalesAgentRuntime(
        gateway=gateway,
        checkpointer=memory.checkpointer,
        settings=settings,
    )
    runtime._memory = memory
    runtime._gateway = gateway
    return runtime


def create_app(*, runtime: Any | None = None, settings: AgentSettings | None = None) -> FastAPI:
    """Create the Sales Agent HTTP process app with injectable runtime seams."""
    app = FastAPI(title="OdontoFlow Sales Agent", version="0.1.0")
    app.state.sales_agent_runtime = runtime
    app.state.sales_agent_settings = settings

    @app.post("/sales-agent/turn", response_model=SalesAgentTurnResponse)
    def sales_agent_turn(payload: SalesAgentTurnRequest, request: Request):
        active_runtime = getattr(request.app.state, "sales_agent_runtime", None)
        if active_runtime is None:
            active_settings = (
                getattr(request.app.state, "sales_agent_settings", None) or get_settings()
            )
            try:
                active_runtime = _build_runtime(active_settings)
            except AgentUnavailableError:
                return _error_response(
                    "AGENT_UNAVAILABLE",
                    "The Sales Agent runtime is not installed.",
                    503,
                )
            request.app.state.sales_agent_runtime = active_runtime
        try:
            return active_runtime.turn(payload)
        except GatewayError as exc:
            return _error_response(exc.code, exc.message, exc.status_code or 502)
        except AgentUnavailableError:
            return _error_response(
                "AGENT_UNAVAILABLE",
                "The Sales Agent runtime is not installed.",
                503,
            )
        except ValueError:
            return _error_response(
                "INVALID_AGENT_RESPONSE",
                "The Sales Agent returned an invalid structured response.",
                502,
            )
        except RuntimeError:
            return _error_response(
                "AGENT_EXECUTION_FAILED",
                "The Sales Agent could not complete this turn safely.",
                503,
            )

    return app


app = create_app()


__all__ = ["app", "create_app"]
