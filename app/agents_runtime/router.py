"""COB HTTP surface: 'ejecutar ahora' for the collections agent, and run history.

BACKFILL adds ``POST /agent-runs/jobs/run-due`` (the explicit job tick, called by
the CLI or n8n) on this already-mounted router.

Thin by contract: HTTP shape → schema → service → ``AgentRunOut``. Mounted in
the authenticated loop of ``app/__init__.py``; the service owns every
transaction (the router runs no read before it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.agent_jobs.service import run_due
from app.agents_runtime.schemas import (
    AgentRunCreate,
    AgentRunOut,
    AgentRunPage,
    JobOut,
    JobsRunDue,
    JobsRunOut,
    RunAgentKey,
)
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
    agent_key: RunAgentKey | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AgentRunPage:
    ctx = resolve_http_context(request)
    return list_runs(db, ctx=ctx, agent_key=agent_key, limit=limit)


@router.post("/jobs/run-due", response_model=JobsRunOut, status_code=200)
def run_due_jobs_route(
    payload: JobsRunDue,
    request: Request,
    db: Session = Depends(get_db),
) -> JobsRunOut:
    """BACKFILL tick: enqueue due jobs from domain events, claim, handle, settle.

    Safe to repeat without an ``Idempotency-Key`` (unique ``job_key`` + leases).
    """
    ctx = resolve_http_context(request)
    result = run_due(db, ctx=ctx, limit=payload.limit)
    return JobsRunOut(
        enqueued=result.enqueued,
        claimed=result.claimed,
        done=result.done,
        failed=result.failed,
        dead=result.dead,
        lost=result.lost,
        disabled_agents=result.disabled_agents,
        jobs=[JobOut(**{name: getattr(job, name) for name in JobOut.model_fields})
              for job in result.jobs],
    )
