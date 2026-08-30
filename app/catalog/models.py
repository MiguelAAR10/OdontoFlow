from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_services_organization"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    public_description: Mapped[str | None] = mapped_column(Text)
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="PEN")
    booking_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="automatic"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="ck_services_positive_duration"),
        CheckConstraint(
            "base_price IS NULL OR base_price >= 0",
            name="ck_services_nonnegative_base_price",
        ),
        CheckConstraint(
            "booking_mode IN ('automatic', 'evaluation_first', 'human_only')",
            name="ck_services_booking_mode",
        ),
        # The catalog is per tenant: two organizations may both sell "Limpieza",
        # one organization may not list it twice. Replaces the global name UNIQUE.
        UniqueConstraint("organization_id", "name", name="uq_services_organization_name"),
        # Tenant-qualified referenced key for every service-scoped child (§7.1).
        UniqueConstraint("organization_id", "id", name="uq_services_organization_id"),
    )


class Promotion(Base):
    """Public, date-bounded offer the receptionist may quote verbatim."""

    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    promotional_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="PEN")
    service_id: Mapped[int | None] = mapped_column()
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)
    new_patients_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("valid_until >= valid_from", name="ck_promotions_valid_dates"),
        CheckConstraint(
            "promotional_price IS NULL OR promotional_price >= 0",
            name="ck_promotions_nonnegative_price",
        ),
        CheckConstraint(
            "discount_percent IS NULL OR "
            "(discount_percent >= 0 AND discount_percent <= 100)",
            name="ck_promotions_discount_percent",
        ),
        CheckConstraint("priority >= 0", name="ck_promotions_priority"),
        UniqueConstraint("organization_id", "id", name="uq_promotions_organization_id"),
        UniqueConstraint("organization_id", "code", name="uq_promotions_organization_code"),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_promotions_organization_service",
        ),
    )
