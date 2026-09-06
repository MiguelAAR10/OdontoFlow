"""PF5 — Clinical core domain (Patient, Visit, ServiceExecution).

Three tables, each organization-owned directly (PF0 P10/T1) with the §7
composite-FK pattern: every cross-tenant relationship is structurally
impossible at the database level. ``Practitioner`` stays global and reaches a
visit only through its organization membership (same rule as scheduling).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

#: The closed sex vocabulary inherited from the legacy domain (M/F/O).
SEXO_CHECK = "sexo IN ('M', 'F', 'O')"


class Patient(Base):
    """An organization-owned clinic patient (PF0 P10).

    ``dni`` is the durable clinic identity: unique per organization when
    present (partial unique index — PostgreSQL treats NULLs as distinct, so
    many patients without DNI are legal). ``birth_date`` is a single nullable
    date; the legacy year/month/day split is not carried over (the partial
    case is deferred until a policy demands it).
    """

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_patients_organization"),
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dni: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sexo: Mapped[str | None] = mapped_column(String(10), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(25), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(SEXO_CHECK, name="ck_patients_sexo"),
        # Tenant-qualified referenced key every clinical child points at (§7.1).
        UniqueConstraint("organization_id", "id", name="uq_patients_organization_id"),
        # The durable clinic identity, per organization (PF0 P10; legacy DNI
        # uniqueness adapted to the tenant boundary). Partial: NULLs distinct.
        Index(
            "uq_patients_org_dni",
            "organization_id",
            "dni",
            unique=True,
            postgresql_where=text("dni IS NOT NULL"),
        ),
        Index("ix_patients_org_name", "organization_id", "full_name"),
    )


class Visit(Base):
    """An attended clinical encounter — not a reservation.

    Optionally originates from a confirmed ``Appointment`` (the proven domain
    rule: attendance realizes a confirmed reservation; the practitioner and
    location are then derived from it). Without an appointment, the visit is a
    walk-in and requires an explicit practitioner and location. ``started_at``
    is the domain-owned attendance instant (server default), never supplied by
    the client.
    """

    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_visits_organization"),
        nullable=False,
    )
    patient_id: Mapped[int] = mapped_column(nullable=False)
    appointment_id: Mapped[int | None] = mapped_column(nullable=True)
    practitioner_id: Mapped[int] = mapped_column(nullable=False)
    location_id: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    patient: Mapped[Patient] = relationship(foreign_keys=[patient_id])
    appointment: Mapped["Appointment | None"] = relationship(foreign_keys=[appointment_id])  # noqa: F821
    # No direct FK to practitioners/locations (the composite FKs go through
    # memberships/locations tenant keys), so the joins are explicit.
    practitioner: Mapped["Practitioner"] = relationship(  # noqa: F821
        foreign_keys=[practitioner_id],
        primaryjoin="Practitioner.id == Visit.practitioner_id",
    )
    location: Mapped["Location"] = relationship(  # noqa: F821
        foreign_keys=[location_id],
        primaryjoin="Location.id == Visit.location_id",
    )
    executions: Mapped[list["ServiceExecution"]] = relationship(
        back_populates="visit", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_visits_organization_id"),
        Index(
            "uq_visits_org_appointment",
            "organization_id",
            "appointment_id",
            unique=True,
            postgresql_where=text("appointment_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["organization_id", "patient_id"],
            ["patients.organization_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_patient",
        ),
        # MATCH SIMPLE on purpose (§7.3): a visit without an appointment skips
        # the check; an appointment of another organization is rejected.
        ForeignKeyConstraint(
            ["organization_id", "appointment_id"],
            ["appointments.organization_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_appointment",
        ),
        ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_location",
        ),
    )


class ServiceExecution(Base):
    """One service actually performed during a visit.

    References the canonical ``Service`` catalog with the §7 composite FK and
    records ``executed_price`` — the point-in-time price snapshot inherited
    from the legacy domain: the row owns its price forever, so later catalog
    changes never affect a recorded execution. A service executes at most once
    per visit (``UNIQUE (organization_id, visit_id, service_id)``), the same
    rule the legacy domain enforced.
    """

    __tablename__ = "service_executions"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_service_executions_organization",
        ),
        nullable=False,
    )
    visit_id: Mapped[int] = mapped_column(nullable=False)
    service_id: Mapped[int] = mapped_column(nullable=False)
    executed_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    visit: Mapped[Visit] = relationship(back_populates="executions")
    service: Mapped["Service"] = relationship(foreign_keys=[service_id])  # noqa: F821

    __table_args__ = (
        CheckConstraint("executed_price >= 0", name="ck_service_executions_price"),
        UniqueConstraint("organization_id", "id", name="uq_service_executions_organization_id"),
        # The legacy per-visit service uniqueness, tenant-qualified.
        UniqueConstraint(
            "organization_id",
            "visit_id",
            "service_id",
            name="uq_service_executions_org_visit_service",
        ),
        ForeignKeyConstraint(
            ["organization_id", "visit_id"],
            ["visits.organization_id", "visits.id"],
            ondelete="RESTRICT",
            name="fk_service_executions_organization_visit",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_service_executions_organization_service",
        ),
    )
