"""PF3 — ExecutionContext resolution and the HTTP transport adapter (PF0 §13).

The value object itself lives in ``app.iam.context`` (PF2 introduced the type,
§13 X8). This module is where PF3 makes it the application-boundary contract:
the single function that resolves a context at the FastAPI boundary, plus the
trusted/default context that keeps the pre-auth fixtures working.

Per PF0 §13 X3 the HTTP adapter derives:

* ``request_id`` — generated per request (uuid4 hex), never read from a header;
* ``correlation_id`` — the ``X-Correlation-Id`` header when present, else
  ``request_id`` (X5: never NULL again);
* ``organization_id`` / ``principal_id`` / ``principal_type`` — from the
  identity binding (BLOCKER-1). PF3 is **not** login, so the binding is the
  trusted/default one: the seeded ``system`` principal in the bootstrap
  organization. ``principal_type`` is read from the ``principals`` row, never
  from a header or body field (PR4/F-9).

The default context is what keeps all 258 pre-PF3 fixtures green: a transport
that has no identity yet resolves to the platform ``system`` actor, which the
migration seeded with the whole permission catalog in every existing
organization (PR7). Real authentication later replaces this one seam without
touching the services.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Request

from app.iam.context import ExecutionContext
from app.iam.models import SYSTEM_PRINCIPAL_ID, SYSTEM_PRINCIPAL_TYPE
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

#: The only transport header PF0 §13 defines for the HTTP adapter.
CORRELATION_HEADER = "X-Correlation-Id"


def new_request_id() -> str:
    """One unique request identifier per transport invocation (uuid4 hex, X3)."""
    return uuid4().hex


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


def resolve_http_context(request: Request) -> ExecutionContext:
    """Derive the context for one HTTP request (PF0 §13 X3).

    ``request_id`` is always generated per request; ``correlation_id`` is the
    ``X-Correlation-Id`` header when present, else ``request_id``. Identity is
    the trusted/default binding (PF3 is **not** login): the seeded ``system``
    principal in the bootstrap organization. ``principal_type`` is the seeded
    row's value (PR4) — a caller cannot change the recorded principal type with
    any header or body field (F-9).
    """
    request_id = new_request_id()
    correlation_id = request.headers.get(CORRELATION_HEADER) or request_id
    return ExecutionContext(
        organization_id=BOOTSTRAP_ORGANIZATION_ID,
        principal_id=SYSTEM_PRINCIPAL_ID,
        principal_type=SYSTEM_PRINCIPAL_TYPE,
        request_id=request_id,
        correlation_id=correlation_id,
    )
