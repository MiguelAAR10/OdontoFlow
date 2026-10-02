"""``agent_proposals``: what an agent proposes and a human approves (B2).

Mirrors migrations ``0021``, ``0025`` (INV: inventory kinds) and ``0026``
(BACKFILL: ``waitlist_offer``); PostgreSQL owns every invariant (closed kind and
status vocabularies, decider/result/error coherence, one open proposal per
subject, tenant-consistent composite FKs).
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PROPOSAL_KINDS = (
    "collection_reminder",
    "collection_follow_up",
    "inventory_transfer",
    "inventory_entry",
    "waitlist_offer",
)
PROPOSAL_STATUSES = (
    "pending",
    "approved",
    "executed",
    "failed",
    "declined",
    "expired",
    "superseded",
)
OPEN_STATUSES = ("pending", "approved")
DEDUPE_INDEX = "uq_agent_proposals_open_dedupe"


class AgentProposal(Base):
    __tablename__ = "agent_proposals"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_agent_proposals_organization"),
        nullable=False,
    )
    location_id: Mapped[int | None] = mapped_column()
    conversation_id: Mapped[int | None] = mapped_column()
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject_version: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    execution_key: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    proposed_by_principal_id: Mapped[int] = mapped_column(nullable=False)
    decided_by_principal_id: Mapped[int | None] = mapped_column()
    decision_note: Mapped[str | None] = mapped_column(Text)
    result_ref: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('collection_reminder', 'collection_follow_up', 'inventory_transfer', "
            "'inventory_entry', 'waitlist_offer')",
            name="ck_agent_proposals_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'executed', 'failed', 'declined', 'expired', "
            "'superseded')",
            name="ck_agent_proposals_status",
        ),
        UniqueConstraint("organization_id", "id", name="uq_agent_proposals_organization_id"),
        UniqueConstraint("execution_key", name="uq_agent_proposals_execution_key"),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_agent_proposals_organization_location",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_agent_proposals_organization_conversation",
        ),
        ForeignKeyConstraint(
            ["organization_id", "proposed_by_principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_agent_proposals_organization_proposer",
        ),
        ForeignKeyConstraint(
            ["organization_id", "decided_by_principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_agent_proposals_organization_decider",
        ),
        Index(
            DEDUPE_INDEX,
            "organization_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )
