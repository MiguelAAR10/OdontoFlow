"""COB HTTP surface: 'ejecutar ahora' for the collections agent, and run history.

Thin by contract: HTTP shape → schema → service → ``AgentRunOut``. Mounted in
the authenticated loop of ``app/__init__.py``; the service owns every
transaction (the router runs no read before it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.agents_runtime.schemas import AgentKey, AgentRunCreate, AgentRunOut, AgentRunPage
from app.agents_runtime.service import get_run, list_runs, run_agent
from app.context import resolve_http_context
from app.db import get_db

router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get(IDEMPOTENCY_HEADER)
    return value or None


@router.post("", response_model=AgentRunOut, status_code=201)
def create_run_route(
    payload: AgentRunCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AgentRunOut:
    ctx = resolve_http_context(request)
    result = run_agent(db, ctx=ctx, agent_key=payload.agent_key, key=_idempotency_key(request))
    if result.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return get_run(db, ctx=ctx, run_id=result.run_id)


@router.get("", response_model=AgentRunPage)
def list_runs_route(
    request: Request,
    agent_key: AgentKey | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AgentRunPage:
    ctx = resolve_http_context(request)
    return list_runs(db, ctx=ctx, agent_key=agent_key, limit=limit)
