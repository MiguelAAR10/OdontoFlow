"""B3: one ``agent_runs`` row per reception (Sales Agent) turn.

Called by ``sales_agent/api.py`` in its own transaction after the turn's
outcome is known. No audit row: the activity feed reads ``agent_runs``
directly. PostgreSQL enforces that the cited message belongs to that
conversation in that tenant (``fk_agent_runs_organization_trigger_message``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.agents_runtime.models import AgentRun
from app.iam.context import ExecutionContext

AGENT_KEY = "reception"


def record_reception_turn(
    session: Session,
    ctx: ExecutionContext,
    *,
    conversation_id: int,
    trigger_message_id: int,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    error_category: str | None = None,
) -> int:
    failed = status == "failed"
    with session.begin():
        run = AgentRun(
            organization_id=ctx.organization_id,
            agent_key=AGENT_KEY,
            trigger="event",
            status="failed" if failed else "completed",
            triggered_by_principal_id=ctx.principal_id,
            error_category=(error_category or "unexpected") if failed else None,
            started_at=started_at,
            finished_at=max(finished_at, started_at),
            conversation_id=conversation_id,
            trigger_message_id=trigger_message_id,
        )
        session.add(run)
        session.flush()
        return run.id
