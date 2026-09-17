"""FastAPI entrypoint for the optional Sales Agent process."""

from __future__ import annotations

import json
import logging
import secrets
from time import perf_counter_ns
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.config import get_settings as get_backend_settings
from app.context import INTEGRATION_BEARER, require_authenticated_context
from app.db import SessionLocal
from app.errors import AppError, register_error_handlers
from app.http_security import SecurityBoundaryMiddleware, install_security_openapi
from app.iam.credentials import authentication_required
from app.iam.permissions import CONVERSATIONS_READ
from app.iam.service import (
    PERMISSION_DENIED_HTTP_STATUS,
    PERMISSION_DENIED_MESSAGE,
    IamErrorCode,
    require_permission,
)
from sales_agent.config import AgentSettings, get_settings
from sales_agent.schemas import (
    AgentUnavailableError,
    GatewayError,
    SalesAgentTurnRequest,
    SalesAgentTurnResponse,
)

diagnostic_logger = logging.getLogger("sales_agent.diagnostics")


def _configured_service_credential(request: Request) -> str | None:
    """Return the one server-configured identity allowed to run this process.

    The caller is still authenticated by PostgreSQL through
    ``require_authenticated_context``. This additional binding prevents a
    credential from another tenant from driving a runtime whose backend
    gateway is configured for this process's tenant. If an injected runtime
    and settings disagree, fail closed rather than choosing one identity.
    """
    active_settings = getattr(request.app.state, "sales_agent_settings", None)
    if active_settings is None:
        active_settings = get_settings()
    configured = active_settings.backend_credential

    runtime = getattr(request.app.state, "sales_agent_runtime", None)
    gateway = getattr(runtime, "gateway", None)
    if gateway is not None:
        runtime_credential = getattr(gateway, "credential", None)
        if not isinstance(runtime_credential, str) or not runtime_credential.strip():
            return None
        if configured is not None and not secrets.compare_digest(
            configured, runtime_credential
        ):
            return None
        configured = runtime_credential

    return configured if isinstance(configured, str) and configured.strip() else None


def _authorize_sales_agent_turn(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(INTEGRATION_BEARER),
) -> None:
    """Authorize the configured agent service before the runtime is invoked."""
    context = getattr(request.state, "execution_context", None)
    configured = _configured_service_credential(request)
    if (
        context is None
        or credentials is None
        or configured is None
        or not secrets.compare_digest(credentials.credentials, configured)
    ):
        raise authentication_required()

    if context.principal_type != "agent":
        raise AppError(
            IamErrorCode.PERMISSION_DENIED,
            PERMISSION_DENIED_MESSAGE,
            details={},
            http_status=PERMISSION_DENIED_HTTP_STATUS,
        )

    maker = getattr(request.app.state, "auth_sessionmaker", None) or SessionLocal
    with maker() as session:
        require_permission(session, context, CONVERSATIONS_READ)


def _safe_trace_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (AttributeError, ValueError):
        return None


def _trace_details(request: Request) -> dict[str, str]:
    details: dict[str, str] = {}
    request_id = _safe_trace_id(getattr(request.state, "request_id", None))
    correlation_id = _safe_trace_id(getattr(request.state, "correlation_id", None))
    if request_id is not None:
        details["request_id"] = request_id
    if correlation_id is not None:
        details["correlation_id"] = correlation_id
    return details


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (perf_counter_ns() - started_ns) // 1_000_000)


def _record_failure(request: Request, exc: BaseException, *, elapsed_ms: int) -> None:
    from sales_agent.runtime import SalesAgentDiagnostic, diagnostic_for_exception

    diagnostic = getattr(exc, "diagnostic", None)
    if not isinstance(diagnostic, SalesAgentDiagnostic):
        diagnostic = diagnostic_for_exception(
            exc,
            stage="request_boundary",
        )
    payload = {
        **_trace_details(request),
        **diagnostic.as_dict(elapsed_ms=elapsed_ms),
    }
    diagnostic_logger.warning(
        "sales_agent_failure %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _error_response(
    code: str,
    message: str,
    status_code: int,
    *,
    details: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
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
    security_settings = get_backend_settings()
    app.state.security_settings = security_settings
    app.state.sales_agent_runtime = runtime
    app.state.sales_agent_settings = settings
    register_error_handlers(app)
    app.add_middleware(SecurityBoundaryMiddleware, settings=security_settings)

    @app.post(
        "/sales-agent/turn",
        response_model=SalesAgentTurnResponse,
        dependencies=[
            Depends(require_authenticated_context),
            Depends(_authorize_sales_agent_turn),
        ],
    )
    def sales_agent_turn(payload: SalesAgentTurnRequest, request: Request):
        started_ns = perf_counter_ns()
        active_runtime = getattr(request.app.state, "sales_agent_runtime", None)
        if active_runtime is None:
            active_settings = (
                getattr(request.app.state, "sales_agent_settings", None) or get_settings()
            )
            try:
                active_runtime = _build_runtime(active_settings)
            except AgentUnavailableError as exc:
                _record_failure(request, exc, elapsed_ms=_elapsed_ms(started_ns))
                return _error_response(
                    "AGENT_UNAVAILABLE",
                    "The Sales Agent runtime is not installed.",
                    503,
                    details=_trace_details(request),
                )
            request.app.state.sales_agent_runtime = active_runtime
        try:
            return active_runtime.turn(payload)
        except GatewayError as exc:
            _record_failure(request, exc, elapsed_ms=_elapsed_ms(started_ns))
            return _error_response(
                exc.code,
                exc.message,
                exc.status_code or 502,
                details=_trace_details(request),
            )
        except AgentUnavailableError as exc:
            _record_failure(request, exc, elapsed_ms=_elapsed_ms(started_ns))
            return _error_response(
                "AGENT_UNAVAILABLE",
                "The Sales Agent runtime is not installed.",
                503,
                details=_trace_details(request),
            )
        except ValueError as exc:
            _record_failure(request, exc, elapsed_ms=_elapsed_ms(started_ns))
            return _error_response(
                "INVALID_AGENT_RESPONSE",
                "The Sales Agent returned an invalid structured response.",
                502,
                details=_trace_details(request),
            )
        except RuntimeError as exc:
            _record_failure(request, exc, elapsed_ms=_elapsed_ms(started_ns))
            return _error_response(
                "AGENT_EXECUTION_FAILED",
                "The Sales Agent could not complete this turn safely.",
                503,
                details=_trace_details(request),
            )

    install_security_openapi(app)
    return app


app = create_app()


__all__ = ["app", "create_app"]
