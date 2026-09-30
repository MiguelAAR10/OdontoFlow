"""Stage one domain event inside the caller's transaction."""

from typing import Any

from sqlalchemy.orm import Session

from app.events.models import DomainEvent
from app.iam.context import ExecutionContext


def record_domain_event(
    session: Session,
    *,
    ctx: ExecutionContext,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> DomainEvent:
    """Add and flush one event; never begins, commits or rolls back.

    Like ``record_event`` it is atomic with the mutation that emits it: a
    rollback of the caller's transaction removes the event too.
    """
    event = DomainEvent(
        organization_id=ctx.organization_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload=payload,
        correlation_id=ctx.correlation_id,
    )
    session.add(event)
    session.flush()
    return event
