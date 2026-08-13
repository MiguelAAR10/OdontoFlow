from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    acquisition_source: Mapped[str] = mapped_column(String(20), nullable=False)
    service_need_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    commercial_status: Mapped[str] = mapped_column(String(30), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    service_need: Mapped["Service | None"] = relationship()  # noqa: F821

    __table_args__ = (
        CheckConstraint(
            "acquisition_source IN ('promotion', 'referral', 'direct')",
            name="ck_leads_acquisition_source",
        ),
        CheckConstraint(
            "contact_phone IS NOT NULL OR contact_email IS NOT NULL",
            name="ck_leads_at_least_one_contact",
        ),
    )
