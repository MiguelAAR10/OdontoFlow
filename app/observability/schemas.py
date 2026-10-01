"""HTTP contract of the B3 activity feed and productivity report."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

ActivitySource = Literal["audit", "proposal", "agent_run"]
_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class ActivityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: int | None = Field(default=None, ge=1)
    agent_key: str | None = Field(default=None, min_length=1, max_length=40)
    since: AwareDatetime | None = None
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)


class ActivityItem(BaseModel):
    """One thing that happened. ``summary`` never carries state JSON (PII)."""

    source: ActivitySource
    id: int
    occurred_at: datetime
    action: str
    entity_type: str
    entity_id: str
    actor_kind: str
    actor_principal_id: int | None
    actor_display_name: str
    agent_key: str | None
    location_id: int | None
    summary: str


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    next_cursor: str | None


class ProductivityQuery(BaseModel):
    """``from``/``to`` are plain ISO dates (inclusive), local to each row's location."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    location_id: int | None = Field(default=None, ge=1)

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _plain_date(cls, value):
        import re

        if not isinstance(value, str) or not re.match(_ISO_DATE, value):
            raise ValueError("Use an ISO date (YYYY-MM-DD).")
        return value


class AppointmentCounts(BaseModel):
    completed: int
    no_show: int
    cancelled: int


class MoneyTotals(BaseModel):
    currency: Literal["PEN"] = "PEN"
    charged: str
    collected: str
    outstanding: str


class AgentProposalCounts(BaseModel):
    agent_key: str
    created: int
    approved: int
    declined: int
    expired: int


class ProductivityReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(serialization_alias="from")
    to: date
    location_id: int | None
    appointments: AppointmentCounts
    money: MoneyTotals
    proposals: list[AgentProposalCounts]
    collection_reminders_approved: int
