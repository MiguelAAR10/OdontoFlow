from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Identity, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_organization", "organization_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    # Plain FK on purpose (PF0 D6): audit records history — including events
    # about membership itself — and must never be blocked by membership
    # topology, so it does not use the composite tenant FK pattern.
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_audit_events_organization"),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str | None] = mapped_column(String(100))


class SecurityEvent(Base):
    """Security telemetry, including failures without a trusted tenant."""

    __tablename__ = "security_events"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'blocked')",
            name="ck_security_events_outcome",
        ),
        Index("ix_security_events_occurred_at", "occurred_at"),
        Index(
            "ix_security_events_organization",
            "organization_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_security_events_organization",
        )
    )
    principal_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "principals.id",
            ondelete="RESTRICT",
            name="fk_security_events_principal",
        )
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
