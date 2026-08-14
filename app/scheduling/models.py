from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AvailabilityRule(Base):
    __tablename__ = "availability_rules"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id", ondelete="RESTRICT", name="fk_availability_rules_organization"
        ),
        nullable=False,
    )
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    start_local: Mapped[time] = mapped_column(Time, nullable=False)
    end_local: Mapped[time] = mapped_column(Time, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    practitioner: Mapped[Practitioner] = relationship(foreign_keys=[practitioner_id])  # noqa: F821
    location: Mapped[Location] = relationship(foreign_keys=[location_id])  # noqa: F821

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_availability_rules_weekday"),
        CheckConstraint("end_local > start_local", name="ck_availability_rules_interval"),
        ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_availability_rules_organization_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_availability_rules_organization_location",
        ),
        Index(
            "ix_availability_rules_organization_location", "organization_id", "location_id"
        ),
    )


class ScheduleBlock(Base):
    __tablename__ = "schedule_blocks"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id", ondelete="RESTRICT", name="fk_schedule_blocks_organization"
        ),
        nullable=False,
    )
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    practitioner: Mapped[Practitioner] = relationship(foreign_keys=[practitioner_id])  # noqa: F821
    location: Mapped[Location] = relationship(foreign_keys=[location_id])  # noqa: F821

    __table_args__ = (
        CheckConstraint("end_utc > start_utc", name="ck_schedule_blocks_interval"),
        ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_schedule_blocks_organization_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_schedule_blocks_organization_location",
        ),
        Index("ix_schedule_blocks_organization_location", "organization_id", "location_id"),
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_appointments_organization"),
        nullable=False,
    )
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"), nullable=False)
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    lead: Mapped[Lead] = relationship(foreign_keys=[lead_id])  # noqa: F821
    service: Mapped[Service] = relationship(foreign_keys=[service_id])  # noqa: F821
    practitioner: Mapped[Practitioner] = relationship(foreign_keys=[practitioner_id])  # noqa: F821
    location: Mapped[Location] = relationship(foreign_keys=[location_id])  # noqa: F821

    __table_args__ = (
        CheckConstraint("state IN ('confirmed', 'cancelled')", name="ck_appointments_state"),
        CheckConstraint("end_utc > start_utc", name="ck_appointments_interval"),
        # Partial GiST exclusion: only confirmed appointments consume the
        # practitioner's schedule. Cancelled rows never block interval reuse.
        #
        # PF1 keeps the key PRACTITIONER-GLOBAL on purpose (PF0 §9 S1): a
        # practitioner shared by two organizations cannot be in two chairs at
        # 15:00, so ``organization_id`` must NEVER join this key. Multi-tenancy
        # is a visibility boundary, not a licence to violate physics.
        ExcludeConstraint(
            (text("practitioner_id"), "="),
            (text("tstzrange(start_utc, end_utc, '[)')"), "&&"),
            where=text("state = 'confirmed'"),
            name="excl_appointments_confirmed_no_overlap",
        ),
        # Tenant-qualified referenced key for future clinical/finance children.
        UniqueConstraint("organization_id", "id", name="uq_appointments_organization_id"),
        # The canonical §7 case: no mix of tenants across lead / service /
        # practitioner / location is expressible at all.
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            ondelete="RESTRICT",
            name="fk_appointments_organization_lead",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_appointments_organization_service",
        ),
        ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_appointments_organization_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_appointments_organization_location",
        ),
    )
