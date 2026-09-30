"""HTTP contract for agent runs (COB)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

AgentKey = Literal["cobranza"]
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
    agent_key: AgentKey
    trigger: RunTrigger
    status: RunStatus
    triggered_by_principal_id: int
    counts: AgentRunCounts
    error_category: str | None
    started_at: datetime
    finished_at: datetime | None


class AgentRunPage(BaseModel):
    items: list[AgentRunOut]
