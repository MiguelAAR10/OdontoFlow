from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Organization(Base):
    """The tenant root and the security boundary (PF0 P2).

    Every tenant-owned row carries ``organization_id`` directly (PF0 T1); no
    ownership is ever derived through a join. ``Practitioner`` is deliberately
    *not* owned here — it is a global professional identity that reaches a
    tenant only through :class:`PractitionerMembership` (PF0 T2/P4).
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_locations_organization"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        # The tenant-qualified referenced key every location-scoped child points
        # at, so a child's tenant and its location's tenant are the same value
        # by construction (PF0 §7.1).
        UniqueConstraint("organization_id", "id", name="uq_locations_organization_id"),
    )


class Practitioner(Base):
    """A GLOBAL professional identity (PF0 P4/T2): no ``organization_id``.

    ``is_active`` here is the platform-wide kill switch; whether the person
    currently works for one organization lives on the membership row (PM3).
    """

    __tablename__ = "practitioners"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(250), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PractitionerMembership(Base):
    """Proof that a practitioner works for an organization (PF0 §8).

    Scheduling rows reference this table by its *natural* tenant key
    ``(organization_id, practitioner_id)`` instead of its surrogate id, so the
    foreign key itself carries the meaning: a practitioner who is not a member
    cannot appear anywhere in that organization's schedule (PM2, F-3b).
    """

    __tablename__ = "practitioner_memberships"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_practitioner_memberships_organization",
        ),
        nullable=False,
    )
    practitioner_id: Mapped[int] = mapped_column(
        ForeignKey(
            "practitioners.id",
            ondelete="RESTRICT",
            name="fk_practitioner_memberships_practitioner",
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "practitioner_id",
            name="uq_practitioner_memberships_org_practitioner",
        ),
        UniqueConstraint("organization_id", "id", name="uq_practitioner_memberships_org_id"),
        Index("ix_practitioner_memberships_practitioner", "practitioner_id"),
    )


class PractitionerCapability(Base):
    __tablename__ = "practitioner_capabilities"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_capabilities_organization"),
        nullable=False,
    )
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    practitioner: Mapped[Practitioner] = relationship(foreign_keys=[practitioner_id])
    service: Mapped[Service] = relationship(foreign_keys=[service_id])  # noqa: F821
    location: Mapped[Location] = relationship(foreign_keys=[location_id])

    __table_args__ = (
        # Unchanged from Vertical 1 (PF0 PM7): ``service_id`` and ``location_id``
        # are now tenant-qualified by the composite FKs below, so the triple is
        # still globally unique and adding ``organization_id`` would be redundant.
        UniqueConstraint(
            "practitioner_id",
            "service_id",
            "location_id",
            name="uq_capabilities_practitioner_service_location",
        ),
        ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_capabilities_organization_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_capabilities_organization_service",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_capabilities_organization_location",
        ),
        Index(
            "ix_capabilities_organization_service_location",
            "organization_id",
            "service_id",
            "location_id",
        ),
    )
