from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="ck_services_positive_duration"),
        # The catalog is per tenant: two organizations may both sell "Limpieza",
        # one organization may not list it twice. Replaces the global name UNIQUE.
        UniqueConstraint("organization_id", "name", name="uq_services_organization_name"),
        # Tenant-qualified referenced key for every service-scoped child (§7.1).
        UniqueConstraint("organization_id", "id", name="uq_services_organization_id"),
    )
