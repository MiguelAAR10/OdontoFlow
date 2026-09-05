"""PF3+ — ExecutionContext resolution and the HTTP transport adapter.

Identity used to be a constant here: every HTTP request resolved to the seeded
``system`` principal in the bootstrap organization, which migration ``0003``
granted the whole permission catalog. The authorization layer was complete and
proven; there was no door in front of it, so an anonymous caller was a
superuser.

``resolve_http_context`` now requires a bearer credential and reads the tenant
and the principal **from PostgreSQL**. Three properties follow:

* No header or body field can influence which organization a caller acts in;
  the values below come from the credential row, never from the request.
* ``system`` is no longer reachable over HTTP. It keeps its catalog for
  migrations, fixtures and scripts through :func:`default_context`, which is
  deliberately *not* used by any router.
* Authentication runs on its **own short-lived session**, never the request
  session. AGENTS.md invariant 4 forbids pre-transaction queries on the session
  a service will call ``session.begin()`` on, and booking depends on receiving
  an idle Session. Sharing it here would have broken that quietly.

The session factory is read from ``app.state.auth_sessionmaker`` when present so
tests can bind authentication to the test engine, exactly as they already
override ``get_db``.
"""

from __future__ import annotations

from enum import Enum

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.audit.service import record_security_event
from app.config import get_settings
from app.db import SessionLocal
from app.errors import AppError
from app.iam.credentials import (
    authenticate,
    authentication_required,
    claim_rate_limit,
)

from app.iam.context import ExecutionContext
from app.iam.models import SYSTEM_PRINCIPAL_ID, SYSTEM_PRINCIPAL_TYPE
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

#: The only transport header PF0 §13 defines for the HTTP adapter.
REQUEST_ID_HEADER = "X-Request-Id"
CORRELATION_HEADER = "X-Correlation-Id"
INTEGRATION_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="IntegrationBearer",
    description="Revocable server-to-server credential. Never embed it in a browser.",
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class TransportSecurityErrorCode(str, Enum):
    RATE_LIMITED = "RATE_LIMITED"
    INTEGRATION_DISABLED = "INTEGRATION_DISABLED"


def _transport_error(
    code: TransportSecurityErrorCode,
    message: str,
    status: int,
    *,
    headers: dict[str, str] | None = None,
):
    return AppError(
        code,  # type: ignore[arg-type]
        message,
        details={},
        http_status=status,
        headers=headers,
    )


def new_request_id() -> str:
    """One canonical UUIDv4 per invocation."""
    from app.http_security import new_uuid

    return new_uuid()


def default_context(organization_id: int | None = None) -> ExecutionContext:
    """The trusted/default execution context (BLOCKER-1 option b).

    The seeded ``system`` principal acting in ``organization_id`` (bootstrap by
    default), with a fresh ``request_id`` and ``correlation_id`` derived from it.
    This is the compatibility context existing fixtures and unauthenticated
    transports resolve to.
    """
    request_id = new_request_id()
    org_id = (
        organization_id if organization_id is not None else BOOTSTRAP_ORGANIZATION_ID
    )
    return ExecutionContext(
        organization_id=org_id,
        principal_id=SYSTEM_PRINCIPAL_ID,
        principal_type=SYSTEM_PRINCIPAL_TYPE,
        request_id=request_id,
        correlation_id=request_id,
    )


def _auth_sessionmaker(request: Request):
    """The factory authentication uses. Overridable for tests, never the request session."""
    return getattr(request.app.state, "auth_sessionmaker", None) or SessionLocal


def require_authenticated_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(INTEGRATION_BEARER),
) -> ExecutionContext:
    """The single authentication gate, applied to every business router.

    Mounted as a router-level dependency in :func:`app.create_app` rather than
    called per endpoint. Four read routes — ``GET /services``,
    ``GET /leads/{id}``, ``GET /practitioners/eligible`` and
    ``POST /slots/query`` — resolved no context at all and were therefore
    unauthenticated *and* unauthorized; those are precisely the first tools the
    agent plan exposes. A gate that must be remembered per endpoint is a gate
    that eventually is not, so this one cannot be omitted by writing a new route.

    The resolved context is cached on ``request.state`` so every endpoint that
    calls :func:`resolve_http_context` reuses it instead of authenticating a
    second time.
    """
    settings = getattr(request.app.state, "security_settings", None) or get_settings()
    if not settings.integration_api_enabled:
        raise _transport_error(
            TransportSecurityErrorCode.INTEGRATION_DISABLED,
            "The integration API is temporarily disabled.",
            503,
        )

    request_id = getattr(request.state, "request_id", None) or new_request_id()
    correlation_id = getattr(request.state, "correlation_id", None) or request_id
    token = credentials.credentials if credentials is not None else None

    maker = _auth_sessionmaker(request)
    session = maker()
    try:
        try:
            credential = authenticate(session, token)
        except AppError:
            record_security_event(
                session,
                event_type="authentication",
                outcome="failed",
                request_id=request_id,
                correlation_id=correlation_id,
            )
            session.commit()
            raise
        principal = session.get(_principal_model(), credential.principal_id)
        category = "read" if request.method in SAFE_METHODS else "mutation"
        limit = (
            settings.rate_limit_reads_per_minute
            if category == "read"
            else settings.rate_limit_mutations_per_minute
        )
        allowed, retry_after = claim_rate_limit(
            session,
            credential_id=credential.id,
            category=category,
            limit=limit,
        )
        if not allowed:
            record_security_event(
                session,
                event_type="rate_limit",
                outcome="blocked",
                request_id=request_id,
                correlation_id=correlation_id,
                organization_id=credential.organization_id,
                principal_id=credential.principal_id,
                metadata={"category": category},
            )
            session.commit()
            raise _transport_error(
                TransportSecurityErrorCode.RATE_LIMITED,
                "The credential rate limit was exceeded.",
                429,
                headers={"Retry-After": str(retry_after)},
            )
        context = ExecutionContext(
            organization_id=credential.organization_id,
            principal_id=credential.principal_id,
            principal_type=principal.type,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        # Successful use is represented by ``last_used_at``. Persisting one
        # security event per ordinary request would turn normal traffic into an
        # unbounded telemetry table; security_events is reserved for failures
        # and blocks that need investigation.
        credential.last_used_at = _now()
        session.commit()
    finally:
        session.close()

    request.state.execution_context = context
    return context


def resolve_http_context(request: Request) -> ExecutionContext:
    """Return the context the authentication gate already resolved.

    Kept as a function with its original signature so the 37 existing call
    sites are untouched. It never re-authenticates: reaching it without a
    cached context means the gate was bypassed, which is a wiring bug, not a
    situation to paper over with a default identity.
    """
    context = getattr(request.state, "execution_context", None)
    if context is None:
        raise authentication_required()
    return context


def _principal_model():
    from app.iam.models import Principal

    return Principal


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
