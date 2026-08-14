"""Append-only audit writer.

``record_event`` only stages the row: it never commits and never opens a
transaction of its own, so the caller's use case decides atomicity. An audit
row therefore lands exactly when — and only when — the state transition it
describes is committed.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.models import AuditEvent

SYSTEM_ACTOR_ID = "system"
SYSTEM_ACTOR_TYPE = "system"


def record_event(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    before_state: dict | None = None,
    after_state: dict | None = None,
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    """Stage one audit row inside the caller's open transaction."""
    event = AuditEvent(
        actor_id=actor_id or SYSTEM_ACTOR_ID,
        actor_type=actor_type or SYSTEM_ACTOR_TYPE,
        action=action,
        entity_id=entity_id,
        entity_type=entity_type,
        before_state=before_state,
        after_state=after_state,
        correlation_id=correlation_id,
    )
    session.add(event)
    return event
