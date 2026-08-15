"""PF4 — the durable command idempotency domain (PF0 spec §15–§16).

``command_receipts`` records what a command *did* (its logical outcome), not
the resource's current state (I6). The row is claimed — and, on success,
settled — inside the command's own transaction, so a committed receipt always
carries its outcome and a rolled-back command leaves no trace (I7/C3).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CommandReceipt(Base):
    """One durable claim per ``(organization_id, operation, idempotency_key)``.

    The unique constraint ``uq_command_receipts_org_operation_key`` is the
    whole concurrency mechanism (I1): two identical commands contend on it,
    one executes and the other replays (C1). ``resource_id`` and
    ``outcome_json`` are NULL on the initial claim and filled by the same
    transaction before commit (I13), so a committed receipt always carries
    the logical outcome the transport renders (I5).

    Append-only after commit: no application flow updates or deletes a
    committed receipt.
    """

    __tablename__ = "command_receipts"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id", ondelete="RESTRICT", name="fk_command_receipts_organization"
        ),
        nullable=False,
    )
    principal_id: Mapped[int] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_command_receipts_principal"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "operation",
            "idempotency_key",
            name="uq_command_receipts_org_operation_key",
        ),
        ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_command_receipts_organization_membership",
        ),
    )
