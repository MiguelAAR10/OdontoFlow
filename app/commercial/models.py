from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_leads_organization"),
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    acquisition_source: Mapped[str] = mapped_column(String(20), nullable=False)
    service_need_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    commercial_status: Mapped[str] = mapped_column(String(30), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    service_need: Mapped["Service | None"] = relationship(foreign_keys=[service_need_id])  # noqa: F821

    __table_args__ = (
        CheckConstraint(
            "acquisition_source IN ('promotion', 'referral', 'direct')",
            name="ck_leads_acquisition_source",
        ),
        CheckConstraint(
            "contact_phone IS NOT NULL OR contact_email IS NOT NULL",
            name="ck_leads_at_least_one_contact",
        ),
        # Tenant-qualified referenced key used by ``appointments`` (§7.1).
        UniqueConstraint("organization_id", "id", name="uq_leads_organization_id"),
        # MATCH SIMPLE on purpose (§7.3): a lead with no declared service need
        # skips the check, a lead naming another tenant's service is rejected.
        ForeignKeyConstraint(
            ["organization_id", "service_need_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_leads_organization_service_need",
        ),
    )
