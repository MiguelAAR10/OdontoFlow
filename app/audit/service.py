"""Append-only audit writer.

``record_event`` only stages the row: it never commits and never opens a
transaction of its own, so the caller's use case decides atomicity. An audit
row therefore lands exactly when — and only when — the state transition it
describes is committed.

Since PF3, authoritative provenance comes from an ``ExecutionContext`` (PF0
§14 D2/D3): ``organization_id``, ``actor_id`` (the principal id) and
``actor_type`` (the principal type) are read from ``ctx``, so a caller cannot
supply a different actor than the one that was resolved. The legacy keyword
form is retained for pre-PF3 callers such as ``organization.created`` (D7,
self-reference audit) that predate any principal.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.models import AuditEvent, SecurityEvent
from app.iam.context import ExecutionContext

SYSTEM_ACTOR_ID = "system"
SYSTEM_ACTOR_TYPE = "system"


def record_event(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    entity_type: str,
    entity_id: str,
    action: str,
    before_state: dict | None = None,
    after_state: dict | None = None,
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    """Stage one audit row inside the caller's open transaction.

    When ``ctx`` is given, provenance derives from it: ``organization_id``,
    ``actor_id = str(ctx.principal_id)``, ``actor_type = ctx.principal_type``
    and ``correlation_id`` (D2/D3). Tenant attribution must be written at event
    time, because it is unrecoverable afterwards (PF0 F-17).

    The keyword form survives for pre-PF3 callers that have no context yet:
    ``organization.created`` passes the newly created organization's own id
    (D7) and falls back to the legacy ``system`` actor defaults.
    """
    if ctx is not None:
        organization_id = ctx.organization_id
        actor_id = str(ctx.principal_id)
        actor_type = ctx.principal_type
        request_id = ctx.request_id
        correlation_id = ctx.correlation_id

    event = AuditEvent(
        organization_id=organization_id,
        actor_id=actor_id or SYSTEM_ACTOR_ID,
        actor_type=actor_type or SYSTEM_ACTOR_TYPE,
        action=action,
        entity_id=entity_id,
        entity_type=entity_type,
        before_state=before_state,
        after_state=after_state,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    session.add(event)
    return event


def record_security_event(
    session: Session,
    *,
    event_type: str,
    outcome: str,
    request_id: str,
    correlation_id: str,
    organization_id: int | None = None,
    principal_id: int | None = None,
    metadata: dict | None = None,
) -> SecurityEvent:
    """Stage redacted security telemetry, including unattributed failures."""
    event = SecurityEvent(
        organization_id=organization_id,
        principal_id=principal_id,
        event_type=event_type,
        outcome=outcome,
        request_id=request_id,
        correlation_id=correlation_id,
        event_metadata=metadata or {},
    )
    session.add(event)
    return event
