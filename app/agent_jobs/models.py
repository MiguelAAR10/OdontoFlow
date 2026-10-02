"""``agent_jobs``: one durable unit of agent work per domain fact (BACKFILL).

Mirrors migration ``0026``. PostgreSQL owns the invariants: one job per
``(organization_id, job_key)``, a tenant-consistent FK to the source
``domain_events`` row, closed status/agent vocabularies, and "leased iff it
holds a token and a lease deadline".
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

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
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JOB_STATUSES = ("queued", "leased", "done", "failed", "dead")
JOB_AGENT_KEYS = ("backfill",)


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_agent_jobs_organization"),
        nullable=False,
    )
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    job_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[int] = mapped_column(nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "source_event_id"],
            ["domain_events.organization_id", "domain_events.id"],
            ondelete="RESTRICT",
            name="fk_agent_jobs_organization_source_event",
        ),
        UniqueConstraint("organization_id", "id", name="uq_agent_jobs_organization_id"),
        UniqueConstraint("organization_id", "job_key", name="uq_agent_jobs_organization_job_key"),
        CheckConstraint(
            "status IN ('queued', 'leased', 'done', 'failed', 'dead')", name="ck_agent_jobs_status"
        ),
        CheckConstraint("agent_key IN ('backfill')", name="ck_agent_jobs_agent_key"),
        CheckConstraint("attempts >= 0", name="ck_agent_jobs_attempts"),
        CheckConstraint(
            "(status = 'leased') = (lease_token IS NOT NULL AND leased_until IS NOT NULL)",
            name="ck_agent_jobs_lease",
        ),
        Index(
            "ix_agent_jobs_org_due",
            "organization_id",
            "run_after",
            postgresql_where=text("status IN ('queued', 'failed', 'leased')"),
        ),
    )
