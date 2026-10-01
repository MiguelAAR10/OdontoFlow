"""HTTP contract for agent proposals and the unified inbox (durable, frontend F1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: INV: inventory kinds are proposed only by the server-side sweep, so this
#: HTTP body Literal stays closed (evidence is always server facts).
ProposalKindName = Literal["collection_reminder", "collection_follow_up"]
InboxKind = Literal[
    "collection_reminder",
    "collection_follow_up",
    "inventory_transfer",
    "inventory_entry",
    "appointment_booking",
]
InboxSource = Literal["agent_proposal", "appointment_proposal"]
InboxStatus = Literal[
    "pending", "approved", "executed", "failed", "declined", "expired", "superseded"
]
InboxAction = Literal["approve", "decline"]


def _not_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be blank")
    return value


class ProposalCreate(BaseModel):
    """What an agent proposes. ``agent_key``, hash, version, TTL, location and
    dedupe key are server-computed, so any other field is rejected."""

    model_config = ConfigDict(extra="forbid")

    kind: ProposalKindName
    payload: dict[str, Any]
    reason: str = Field(min_length=1, max_length=500)
    evidence: dict[str, Any] | None = None

    _reason = field_validator("reason")(_not_blank)


class ProposalApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str | None = Field(default=None, min_length=1, max_length=500)

    _note = field_validator("note")(_not_blank)


class ProposalDecline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, min_length=1, max_length=500)

    _note = field_validator("note")(_not_blank)


class SubjectRef(BaseModel):
    type: str
    id: str


class DecidedBy(BaseModel):
    id: int
    display_name: str


class InboxItem(BaseModel):
    source: InboxSource
    id: int
    kind: InboxKind
    agent_key: str | None
    status: InboxStatus
    location_id: int | None
    summary: str
    reason: str | None
    facts: dict[str, Any] | None
    evidence: dict[str, Any] | None
    payload: dict[str, Any]
    payload_hash: str | None
    subject: SubjectRef | None
    conversation_id: int | None
    confirmation_token: UUID | None
    expires_at: datetime
    created_at: datetime
    decided_by: DecidedBy | None
    result_ref: dict[str, Any] | None
    error_code: str | None
    actions: list[InboxAction]


class InboxPage(BaseModel):
    items: list[InboxItem]
    next_cursor: str | None
