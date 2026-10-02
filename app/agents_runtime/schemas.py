"""HTTP contract for agent runs (COB)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: SELF adds ``confirmaciones`` (D-1 reminders), a manual sweep like COB; INV
#: adds ``inventario`` (low-stock transfer/entry proposals).
AgentKey = Literal["cobranza", "confirmaciones", "inventario"]
#: B3: reception turns also write ``agent_runs``; only the *output* widens, so
#: ``POST /agent-runs`` never accepts ``reception``.
#: BACKFILL: ``backfill`` runs are written by the job handler (``trigger='event'``),
#: never started by ``POST /agent-runs``.
RunAgentKey = Literal["cobranza", "reception", "confirmaciones", "inventario", "backfill"]
RunStatus = Literal["running", "completed", "failed"]
RunTrigger = Literal["manual", "schedule", "event"]


class AgentRunCreate(BaseModel):
    """Only the agent to run; trigger, principal and counts are server facts."""

    model_config = ConfigDict(extra="forbid")

    agent_key: AgentKey


class AgentRunCounts(BaseModel):
    candidates: int
    proposed: int
    deduped: int
    skipped: int


class AgentRunOut(BaseModel):
    id: int
    agent_key: RunAgentKey
    trigger: RunTrigger
    status: RunStatus
    triggered_by_principal_id: int
    counts: AgentRunCounts
    error_category: str | None
    started_at: datetime
    finished_at: datetime | None


class AgentRunPage(BaseModel):
    items: list[AgentRunOut]


# --- BACKFILL: the job tick (``POST /agent-runs/jobs/run-due``) -----------------------

JobAgentKey = Literal["backfill"]
#: ``lost``: the lease expired and another worker reclaimed the job (fenced).
JobOutcome = Literal["done", "failed", "dead", "lost"]


class JobsRunDue(BaseModel):
    """Only how many jobs this tick may claim; everything else is server state."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=20)


class JobOut(BaseModel):
    id: int
    job_key: str
    agent_key: JobAgentKey
    status: JobOutcome
    attempts: int
    run_id: int | None
    last_error: str | None


class JobsRunOut(BaseModel):
    enqueued: int
    claimed: int
    done: int
    failed: int
    dead: int
    lost: int
    disabled_agents: list[JobAgentKey]
    jobs: list[JobOut]
