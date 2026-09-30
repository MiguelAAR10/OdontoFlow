"""Shared fixtures/helpers for the B0.5 domain-gap suites (no tests here).

Spec: ``docs/superpowers/specs/2026-10-01-erp-b05-domain-gaps.md``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.catalog.models import Service
from app.clinical.schemas import PatientCreate, ServiceExecutionCreate, VisitCreate
from app.clinical.service import create_patient, create_service_execution, create_visit
from app.commercial.models import Lead
from app.context import default_context
from app.db import get_db
from app.economics.schemas import ChargeCreate, PaymentCreate, ProductCreate
from app.economics.service import create_charge, create_payment, create_product
from app.events.models import DomainEvent
from app.iam.context import ExecutionContext
from app.iam.models import Principal
from app.iam.service import add_membership, assign_role, create_principal, create_role, grant_permission
from app.inventory.schemas import EntryCreate
from app.inventory.service import register_entry
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import AvailabilityRule
from app.scheduling.service import book_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
#: A Monday well in the past (completable) and one well in the future.
PAST_MONDAY = date(2026, 8, 10)
FUTURE_MONDAY = date(2027, 1, 4)


def local(hour: int, minute: int = 0, day: date = PAST_MONDAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


@pytest.fixture
def api(migrated_engine):
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)


def seed_booking(session, *, organization_id: int = ORG, suffix: str = "1") -> dict:
    """Service + location + practitioner + lead, Monday 09:00–13:00 availability."""
    service = Service(
        organization_id=organization_id,
        name=f"Limpieza {suffix}",
        duration_minutes=30,
        base_price=Decimal("150.00"),
        currency="PEN",
        is_active=True,
    )
    location = Location(
        organization_id=organization_id, name=f"Sede {suffix}", timezone=LIMA, is_active=True
    )
    practitioner = Practitioner(display_name=f"Dra. {suffix}", is_active=True)
    lead = Lead(
        organization_id=organization_id,
        full_name=f"Paciente {suffix}",
        contact_phone=f"+51999{sum(map(ord, suffix)) % 1_000_000:06d}",
        acquisition_source="direct",
    )
    session.add_all([service, location, practitioner, lead])
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=organization_id, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.flush()
    session.add_all(
        [
            PractitionerCapability(
                organization_id=organization_id,
                practitioner_id=practitioner.id,
                service_id=service.id,
                location_id=location.id,
                is_active=True,
            ),
            AvailabilityRule(
                organization_id=organization_id,
                practitioner_id=practitioner.id,
                location_id=location.id,
                day_of_week=0,
                start_local=time(9),
                end_local=time(13),
            ),
        ]
    )
    session.commit()
    return {
        "organization_id": organization_id,
        "lead_id": lead.id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
    }


def book(session, ids: dict, *, day: date = PAST_MONDAY, hour: int = 9, minute: int = 0) -> int:
    appointment = book_appointment(
        session,
        ctx=default_context(ids["organization_id"]),
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        location_id=ids["location_id"],
        practitioner_id=ids["practitioner_id"],
        start=local(hour, minute, day),
    )
    return appointment.id


def domain_events(session, event_type: str | None = None, aggregate_id: str | None = None):
    statement = select(DomainEvent).order_by(DomainEvent.id)
    if event_type is not None:
        statement = statement.where(DomainEvent.event_type == event_type)
    if aggregate_id is not None:
        statement = statement.where(DomainEvent.aggregate_id == aggregate_id)
    session.expire_all()
    rows = session.scalars(statement).all()
    session.rollback()
    return rows


def actor_ctx(
    session,
    *,
    codes: tuple[str, ...] = (),
    principal_type: str = "human",
    organization_id: int = ORG,
) -> ExecutionContext:
    """A real principal with exactly ``codes`` in ``organization_id``."""
    principal = create_principal(
        session, display_name=f"b05-{principal_type}", principal_type=principal_type
    )
    membership = add_membership(
        session, organization_id=organization_id, principal_id=principal.id
    )
    role = create_role(
        session,
        organization_id=organization_id,
        code=f"b05-role-{principal.id}",
        name="b05 actor",
    )
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(
        session,
        organization_id=organization_id,
        membership_id=membership.id,
        role_id=role.id,
    )
    principal_id = principal.id
    session.rollback()
    persisted_type = session.get(Principal, principal_id).type
    session.rollback()  # leave the session idle: services open their own transaction
    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal_id,
        principal_type=persisted_type,
        request_id=f"b05-{principal_id}",
        correlation_id=f"b05-corr-{principal_id}",
    )


def make_charge(session, ids: dict, *, dni: str = "71000001", price: str = "150.00") -> int:
    """Walk-in visit → one execution → one charge of ``price``; returns charge id."""
    ctx = default_context(ids["organization_id"])
    patient = create_patient(
        session, PatientCreate(full_name=f"Paciente {dni}", dni=dni), ctx=ctx
    )
    visit = create_visit(
        session,
        VisitCreate(
            patient_id=patient.id,
            practitioner_id=ids["practitioner_id"],
            location_id=ids["location_id"],
        ),
        ctx=ctx,
    )
    execution = create_service_execution(
        session,
        visit.id,
        ServiceExecutionCreate(service_id=ids["service_id"], executed_price=Decimal(price)),
        ctx=ctx,
    )
    return create_charge(session, execution.id, ChargeCreate(), ctx=ctx).id


def pay(session, charge_id: int, amount: str, *, organization_id: int = ORG, reference=None) -> int:
    return create_payment(
        session,
        charge_id,
        PaymentCreate(amount=Decimal(amount), method="efectivo", reference=reference),
        ctx=default_context(organization_id),
    ).id


def make_product(session, *, organization_id: int = ORG, name: str = "Anestesia") -> int:
    return create_product(
        session,
        ProductCreate(name=name, unit="cartucho", kind="consumible"),
        ctx=default_context(organization_id),
    ).id


def make_location(session, *, organization_id: int = ORG, name: str = "Sede Stock") -> int:
    location = Location(organization_id=organization_id, name=name, timezone=LIMA, is_active=True)
    session.add(location)
    session.commit()
    return location.id


def stock(session, product_id: int, location_id: int, quantity: str, *, organization_id: int = ORG):
    register_entry(
        session,
        product_id,
        EntryCreate(location_id=location_id, quantity=Decimal(quantity)),
        ctx=default_context(organization_id),
    )
