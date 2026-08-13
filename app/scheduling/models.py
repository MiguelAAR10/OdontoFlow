from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Identity, Integer, String, Time, func, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AvailabilityRule(Base):
    __tablename__ = "availability_rules"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    start_local: Mapped[time] = mapped_column(Time, nullable=False)
    end_local: Mapped[time] = mapped_column(Time, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    practitioner: Mapped[Practitioner] = relationship()  # noqa: F821
    location: Mapped[Location] = relationship()  # noqa: F821

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_availability_rules_weekday"),
        CheckConstraint("end_local > start_local", name="ck_availability_rules_interval"),
    )


class ScheduleBlock(Base):
    __tablename__ = "schedule_blocks"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    practitioner: Mapped[Practitioner] = relationship()  # noqa: F821
    location: Mapped[Location] = relationship()  # noqa: F821

    __table_args__ = (
        CheckConstraint("end_utc > start_utc", name="ck_schedule_blocks_interval"),
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"), nullable=False)
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    lead: Mapped[Lead] = relationship()  # noqa: F821
    service: Mapped[Service] = relationship()  # noqa: F821
    practitioner: Mapped[Practitioner] = relationship()  # noqa: F821
    location: Mapped[Location] = relationship()  # noqa: F821

    __table_args__ = (
        CheckConstraint("state IN ('confirmed', 'cancelled')", name="ck_appointments_state"),
        CheckConstraint("end_utc > start_utc", name="ck_appointments_interval"),
        # Partial GiST exclusion: only confirmed appointments consume the
        # practitioner's schedule. Cancelled rows never block interval reuse.
        ExcludeConstraint(
            (text("practitioner_id"), "="),
            (text("tstzrange(start_utc, end_utc, '[)')"), "&&"),
            where=text("state = 'confirmed'"),
            name="excl_appointments_confirmed_no_overlap",
        ),
    )
