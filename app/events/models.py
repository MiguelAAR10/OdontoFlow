"""``domain_events``: an internal outbox of business facts for reactive agents.

Rows are staged by :func:`app.events.service.record_domain_event` inside the
caller's transaction, next to ``record_event``: the fact exists exactly when the
change committed. Nothing reads them yet (B3/B4).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Identity, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DomainEvent(Base):
    __tablename__ = "domain_events"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_domain_events_organization"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_domain_events_organization_id"),
        Index("ix_domain_events_org_type_id", "organization_id", "event_type", "id"),
    )
