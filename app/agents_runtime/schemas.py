"""HTTP contract for agent runs (COB)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: SELF adds ``confirmaciones`` (D-1 reminders), a manual sweep like COB; INV
#: adds ``inventario`` (low-stock transfer/entry proposals).
AgentKey = Literal["cobranza", "confirmaciones", "inventario"]
#: B3: reception turns also write ``agent_runs``; only the *output* widens, so
#: ``POST /agent-runs`` never accepts ``reception``.
RunAgentKey = Literal["cobranza", "reception", "confirmaciones", "inventario"]
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
