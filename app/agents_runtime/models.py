"""``agent_runs``: one row per agent sweep (COB). Mirrors migration ``0022``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_agent_runs_organization"),
        nullable=False,
    )
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    triggered_by_principal_id: Mapped[int] = mapped_column(nullable=False)
    candidates_count: Mapped[int] = mapped_column(nullable=False, default=0)
    proposed_count: Mapped[int] = mapped_column(nullable=False, default=0)
    deduped_count: Mapped[int] = mapped_column(nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("agent_key IN ('cobranza')", name="ck_agent_runs_agent_key"),
        CheckConstraint("trigger IN ('manual', 'schedule', 'event')", name="ck_agent_runs_trigger"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_agent_runs_status"),
        UniqueConstraint("organization_id", "id", name="uq_agent_runs_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "triggered_by_principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_agent_runs_organization_triggered_by",
        ),
        Index(
            "ix_agent_runs_org_agent_started",
            "organization_id",
            "agent_key",
            text("started_at DESC"),
            text("id DESC"),
        ),
    )
