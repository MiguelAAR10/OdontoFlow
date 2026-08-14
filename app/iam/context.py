"""The explicit execution-context value object (PF0 §13).

PF2 introduces the **type only** (X8): it is the input to
:func:`app.iam.service.require_permission`, and service-level tests construct it
directly (X7). PF3 makes it the mandatory parameter of every mutating
application service, derives it in the transports, and makes it authoritative
for audit.

It is explicit, never ambient (A4/X1): no ``ContextVar``, no thread-local, no
middleware-populated global. An agent tool, a system job and a test all call the
same function with the same argument.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Who is acting, where, and under which invocation — and nothing else.

    **No authority lives here** (X4): no permission set, no role list, no
    ``is_admin`` flag, no entity snapshots. Authority is evaluated live against
    the database inside the command's transaction (§12 E2), which is what makes
    an inactive membership lose everything on the very next command with no
    cache to invalidate (E3).

    ``principal_type`` is read from the ``principals`` row during identity
    resolution and never from a header, body field or tool argument (PR4) — the
    structural defence against an agent presenting itself as a human (F-9).
    """

    organization_id: int
    principal_id: int
    principal_type: str
    request_id: str
    correlation_id: str
