"""PF6 — Economic & operations core proofs against real PostgreSQL.

Proves Product / ServiceConsumption / Charge / Payment: tenant isolation at
every level (composite FKs), quantity and amount validations, unit-price
snapshot immutability, the one-product-per-line and one-charge-per-execution
rules (sequential + concurrent), derived economic state (paid/outstanding),
deterministic overpayment rejection under concurrency, permissions, audit
provenance, PF4 idempotency, and the runtime system-access provisioning fix.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.clinical.models import Patient, ServiceExecution, Visit
from app.clinical.service import (
    create_patient,
    create_service_execution,
    create_visit,
)
from app.commercial.models import Lead
from app.context import default_context
from app.db import get_db
from app.economics.models import Charge, Payment, Product, ServiceConsumption
from app.inventory.service import register_entry
from app.economics.service import (
    OP_CHARGES_CREATE,
    OP_CONSUMPTIONS_CREATE,
    OP_PAYMENTS_CREATE,
    OP_PRODUCTS_CREATE,
    charge_paid_amount,
    create_charge,
    create_payment,
    create_product,
    create_service_consumption,
    list_charges,
    list_consumptions,
    list_payments,
    list_products,
)
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    CHARGES_CREATE,
    CONSUMPTIONS_CREATE,
    PAYMENTS_CREATE,
    PRODUCTS_CREATE,
)
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
)
from app.idempotency.models import CommandReceipt
from app.idempotency.service import run_idempotent_command
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.service import create_organization
from app.scheduling.models import Appointment, AvailabilityRule
from app.scheduling.service import book_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)
RULE_WINDOW = (time(9, 0), time(13, 0))


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed_booking(session, *, organization_id=ORG, name_suffix="1"):
    service = Service(
        organization_id=organization_id,
        name=f"Servicio {name_suffix}",
        duration_minutes=30,
        is_active=True,
    )
    location = Location(
        organization_id=organization_id,
        name=f"Sede {name_suffix}",
        timezone=LIMA,
        is_active=True,
    )
    practitioner = Practitioner(display_name=f"Dra. Ana {name_suffix}", is_active=True)
    lead = Lead(
        organization_id=organization_id,
        full_name=f"Juan Pérez {name_suffix}",
        contact_phone=f"+5199900000{name_suffix}",
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
    session.add(
        PractitionerCapability(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            service_id=service.id,
            location_id=location.id,
            is_active=True,
        )
    )
    session.add(
        AvailabilityRule(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            location_id=location.id,
            day_of_week=0,
            start_local=RULE_WINDOW[0],
            end_local=RULE_WINDOW[1],
        )
    )
    session.commit()
    return {
        "organization_id": organization_id,
        "lead_id": lead.id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
    }


def seed_actor(session, *, organization_id=ORG, codes=()):
    principal = create_principal(session, display_name="actor", principal_type="human")
    membership = add_membership(session, organization_id=organization_id, principal_id=principal.id)
    role = create_role(
        session, organization_id=organization_id, code=f"role-{principal.id}", name="actor"
    )
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(
        session,
        organization_id=organization_id,
        membership_id=membership.id,
        role_id=role.id,
    )
    values = principal.id
    session.rollback()
    return values


def ctx_for(principal_id, organization_id=ORG):
    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal_id,
        principal_type="human",
        request_id="req-econ",
        correlation_id="corr-econ",
    )


def make_patient(session, *, organization_id=ORG, dni="12345678"):
    return create_patient(
        session,
        type("D", (), {"full_name": "Paciente Uno", "dni": dni, "sexo": "M", "phone": None, "birth_date": None})(),
        ctx=default_context(organization_id),
    )


def make_product(session, *, organization_id=ORG, name="Anestesia", unit="ampolla", kind="consumible"):
    return create_product(
        session,
        type("D", (), {"name": name, "unit": unit, "kind": kind})(),
        ctx=default_context(organization_id),
    )


def make_location(session, *, organization_id=ORG, name="Sede Econ"):
    location = Location(
        organization_id=organization_id,
        name=name,
        timezone=LIMA,
        is_active=True,
    )
    session.add(location)
    session.commit()
    return location.id


def make_entry(session, product_id, quantity, *, organization_id=ORG, unit_price=None, location_id=None):
    if location_id is None:
        location_id = make_location(session, organization_id=organization_id)
    return register_entry(
        session,
        product_id,
        type(
            "D",
            (),
            {
                "quantity": Decimal(str(quantity)),
                "unit_price": unit_price,
                "location_id": location_id,
            },
        )(),
        ctx=default_context(organization_id),
    )


def make_execution(session, ids, *, organization_id=None, dni=None):
    organization_id = organization_id or ids["organization_id"]
    patient = make_patient(
        session,
        organization_id=organization_id,
        dni=dni or f"7{organization_id}000001",
    )
    patient_id = patient.id
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient_id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(organization_id),
    )
    visit_id = visit.id
    execution = create_service_execution(
        session,
        visit_id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150.00")})(),
        ctx=default_context(organization_id),
    )
    return execution


# --- Product ----------------------------------------------------------------


def test_product_tenant_isolation_and_kind(session):
    product = make_product(session)
    product_id = product.id
    org_b = create_organization(session, "Otra Clínica").id

    # Same name in another organization is a different product.
    other = create_product(
        session,
        type("D", (), {"name": "Anestesia", "unit": "ampolla", "kind": "consumible"})(),
        ctx=default_context(org_b),
    )
    assert product_id != other.id

    # Duplicate name in the SAME organization → stable 422.
    with pytest.raises(AppError) as exc:
        create_product(
            session,
            type("D", (), {"name": "Anestesia", "unit": "ampolla", "kind": "consumible"})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # Cross-org read is NOT_FOUND.
    from app.economics.service import get_product

    with pytest.raises(AppError) as exc:
        get_product(session, other.id, ctx=default_context(ORG))
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()

    assert session.scalar(select(func.count()).select_from(Product)) == 2
    session.rollback()


def test_product_list_filters_by_kind_and_search(session):
    make_product(session, name="Anestesia", kind="consumible")
    make_product(session, name="Protector Solar", unit="unidad", kind="reventa")

    consumibles = list_products(session, ctx=default_context(ORG), kind="consumible")
    assert len(consumibles) == 1
    found = list_products(session, ctx=default_context(ORG), search="solar")
    assert len(found) == 1
    assert found[0].kind == "reventa"


# --- ServiceConsumption -----------------------------------------------------


def test_consumption_links_execution_and_product_with_snapshot(session):
    ids = seed_booking(session)
    execution = make_execution(session, ids)
    execution_id = execution.id
    product = make_product(session)
    product_id = product.id

    make_entry(session, product_id, Decimal("10.00"), unit_price=Decimal("20.00"), location_id=ids["location_id"])
    consumption = create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_id, "quantity": Decimal("2.00"), "unit_price": Decimal("25.50")})(),
        ctx=default_context(ORG),
    )
    assert consumption.service_execution_id == execution_id
    assert consumption.product_id == product_id
    assert consumption.quantity == Decimal("2.00")
    assert consumption.unit_price == Decimal("25.50")

    # The snapshot survives later catalog changes (product rename).
    product = session.get(Product, product_id)
    product.name = "Anestesia premium"
    session.commit()
    fresh = session.get(ServiceConsumption, consumption.id)
    assert fresh.unit_price == Decimal("25.50")
    assert fresh.quantity == Decimal("2.00")
    session.rollback()


def test_consumption_quantity_must_be_positive_and_price_non_negative(session):
    ids = seed_booking(session)
    execution = make_execution(session, ids)
    execution_id = execution.id
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, Decimal("5.00"), location_id=ids["location_id"])

    with pytest.raises(AppError) as exc:
        create_service_consumption(
            session,
            execution_id,
            type("D", (), {"product_id": product_id, "quantity": Decimal("0"), "unit_price": Decimal("1")})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_service_consumption(
            session,
            execution_id,
            type("D", (), {"product_id": product_id, "quantity": Decimal("1"), "unit_price": Decimal("-1")})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # DB backstop for quantity.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO service_consumptions (organization_id, service_execution_id, product_id, quantity, unit_price) "
                "VALUES (:o, :e, :p, -1, 1)"
            ),
            {"o": ORG, "e": execution_id, "p": product_id},
        )
        session.commit()
    session.rollback()
    assert "ck_service_consumptions_quantity" in str(exc.value)


def test_multiple_consumptions_per_execution_and_duplicate_rule(session):
    ids = seed_booking(session)
    execution = make_execution(session, ids)
    execution_id = execution.id
    product_a = make_product(session, name="Anestesia")
    product_a_id = product_a.id
    product_b = make_product(session, name="Guantes")
    product_b_id = product_b.id
    make_entry(session, product_a_id, Decimal("5.00"), location_id=ids["location_id"])
    make_entry(session, product_b_id, Decimal("5.00"), location_id=ids["location_id"])

    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_a_id, "quantity": Decimal("1"), "unit_price": Decimal("10")})(),
        ctx=default_context(ORG),
    )
    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_b_id, "quantity": Decimal("2"), "unit_price": Decimal("3")})(),
        ctx=default_context(ORG),
    )

    # Same product twice in one execution → stable 422.
    with pytest.raises(AppError) as exc:
        create_service_consumption(
            session,
            execution_id,
            type("D", (), {"product_id": product_a_id, "quantity": Decimal("1"), "unit_price": Decimal("1")})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # A different execution may consume the same product again.
    execution_b = make_execution(session, ids, organization_id=ORG, dni="799999998")
    create_service_consumption(
        session,
        execution_b.id,
        type("D", (), {"product_id": product_a_id, "quantity": Decimal("1"), "unit_price": Decimal("10")})(),
        ctx=default_context(ORG),
    )
    assert session.scalar(select(func.count()).select_from(ServiceConsumption)) == 3
    session.rollback()


def test_cross_tenant_consumption_rejected_by_db(session):
    ids = seed_booking(session)
    org_b = create_organization(session, "Otra Clínica").id
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")
    execution = make_execution(session, ids)
    execution_id = execution.id
    product_b = make_product(session, organization_id=org_b, name="Anestesia B")

    # Org A execution + org B product → composite FK violation.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO service_consumptions (organization_id, service_execution_id, product_id, quantity, unit_price) "
                "VALUES (:o, :e, :p, 1, 1)"
            ),
            {"o": ORG, "e": execution_id, "p": product_b.id},
        )
        session.commit()
    session.rollback()
    assert "fk_service_consumptions_organization_product" in str(exc.value)


def test_concurrent_duplicate_consumption_settles_as_422(migrated_engine, session):
    ids = seed_booking(session)
    execution = make_execution(session, ids)
    execution_id = execution.id
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, Decimal("5.00"), location_id=ids["location_id"])

    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt(key):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            create_service_consumption(
                db,
                execution_id,
                type("D", (), {"product_id": product_id, "quantity": Decimal("1"), "unit_price": Decimal("1")})(),
                ctx=default_context(ORG),
            )
            with guard:
                outcomes.append(("committed", key))
        except AppError as exc:
            db.rollback()
            with guard:
                outcomes.append(("app_error", exc.code, key))
        finally:
            db.close()

    threads = [
        threading.Thread(target=attempt, args=("cons-a",)),
        threading.Thread(target=attempt, args=("cons-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    committed = [o for o in outcomes if o[0] == "committed"]
    rejected = [o for o in outcomes if o[0] == "app_error"]
    assert len(committed) == 1, outcomes
    assert len(rejected) == 1, outcomes
    assert rejected[0][1] == ErrorCode.INVALID_INPUT, outcomes

    db = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)()
    rows = list_consumptions(db, execution_id, ctx=default_context(ORG))
    db.close()
    assert len(rows) == 1


# --- Charge -----------------------------------------------------------------


def test_charge_defaults_to_execution_price_snapshot(session):
    ids = seed_booking(session)
    execution = make_execution(session, ids)
    execution_id = execution.id

    charge = create_charge(
        session,
        execution_id,
        type("D", (), {"amount": None})(),
        ctx=default_context(ORG),
    )
    assert charge.amount == Decimal("150.00")

    # One charge per execution: a second one is rejected.
    with pytest.raises(AppError) as exc:
        create_charge(
            session,
            execution_id,
            type("D", (), {"amount": Decimal("100")})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    assert session.scalar(select(func.count()).select_from(Charge)) == 1
    session.rollback()


def test_charge_tenant_isolation(session):
    ids = seed_booking(session)
    org_b = create_organization(session, "Otra Clínica").id
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")
    execution = make_execution(session, ids)
    execution_id = execution.id
    charge_b = create_charge(
        session,
        make_execution(session, ids_b, organization_id=org_b).id,
        type("D", (), {"amount": Decimal("50")})(),
        ctx=default_context(org_b),
    )

    with pytest.raises(AppError) as exc:
        from app.economics.service import get_charge

        get_charge(session, charge_b.id, ctx=default_context(ORG))
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()

    assert execution_id is not None
    rows = list_charges(session, ctx=default_context(ORG))
    assert len(rows) == 0  # the org A execution has no charge yet


# --- Payment -----------------------------------------------------------------


def test_partial_and_full_payment_and_derived_state(session):
    ids = seed_booking(session)
    execution = make_execution(session, ids)
    charge = create_charge(
        session,
        execution.id,
        type("D", (), {"amount": Decimal("150.00")})(),
        ctx=default_context(ORG),
    )
    charge_id = charge.id
    charge_amount = charge.amount  # capture before any rollback expires it

    first = create_payment(
        session,
        charge_id,
        type("D", (), {"amount": Decimal("50.00"), "method": "Yape"})(),
        ctx=default_context(ORG),
    )
    assert first.amount == Decimal("50.00")

    paid = charge_paid_amount(session, charge_id, ORG)
    session.rollback()  # the read autobegins a transaction
    assert paid == Decimal("50.00")
    assert charge_amount - paid == Decimal("100.00")

    second = create_payment(
        session,
        charge_id,
        type("D", (), {"amount": Decimal("100.00"), "method": "Efectivo"})(),
        ctx=default_context(ORG),
    )
    assert second.id != first.id
    paid = charge_paid_amount(session, charge_id, ORG)
    session.rollback()  # the read autobegins a transaction
    assert paid == Decimal("150.00")
    assert charge_amount - paid == Decimal("0.00")

    rows = list_payments(session, charge_id, ctx=default_context(ORG))
    session.rollback()
    assert len(rows) == 2


def test_overpayment_rejected_deterministically(session):
    ids = seed_booking(session)
    charge = create_charge(
        session,
        make_execution(session, ids).id,
        type("D", (), {"amount": Decimal("100.00")})(),
        ctx=default_context(ORG),
    )
    charge_id = charge.id

    create_payment(
        session,
        charge_id,
        type("D", (), {"amount": Decimal("100.00"), "method": "Efectivo"})(),
        ctx=default_context(ORG),
    )
    with pytest.raises(AppError) as exc:
        create_payment(
            session,
            charge_id,
            type("D", (), {"amount": Decimal("0.01"), "method": "Efectivo"})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "exceeds" in exc.value.message
    session.rollback()

    assert session.scalar(select(func.count()).select_from(Payment)) == 1
    session.rollback()


def test_concurrent_payments_never_overpay(migrated_engine, session):
    """Two simultaneous payments of the same charge: the row lock serializes
    them and the loser is rejected — overpayment is structurally impossible."""
    ids = seed_booking(session)
    charge = create_charge(
        session,
        make_execution(session, ids).id,
        type("D", (), {"amount": Decimal("100.00")})(),
        ctx=default_context(ORG),
    )
    charge_id = charge.id

    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt(key):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            create_payment(
                db,
                charge_id,
                type("D", (), {"amount": Decimal("80.00"), "method": "Yape"})(),
                ctx=default_context(ORG),
            )
            with guard:
                outcomes.append(("committed", key))
        except AppError as exc:
            db.rollback()
            with guard:
                outcomes.append(("app_error", exc.code, key))
        finally:
            db.close()

    threads = [
        threading.Thread(target=attempt, args=("pay-a",)),
        threading.Thread(target=attempt, args=("pay-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    committed = [o for o in outcomes if o[0] == "committed"]
    rejected = [o for o in outcomes if o[0] == "app_error"]
    assert len(committed) == 1, outcomes
    assert len(rejected) == 1, outcomes
    assert rejected[0][1] == ErrorCode.INVALID_INPUT, outcomes

    db = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)()
    paid = charge_paid_amount(db, charge_id, ORG)
    db.close()
    assert paid == Decimal("80.00")


def test_payment_tenant_isolation(session):
    ids = seed_booking(session)
    org_b = create_organization(session, "Otra Clínica").id
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")
    charge_b = create_charge(
        session,
        make_execution(session, ids_b, organization_id=org_b).id,
        type("D", (), {"amount": Decimal("50")})(),
        ctx=default_context(org_b),
    )
    create_payment(
        session,
        charge_b.id,
        type("D", (), {"amount": Decimal("10"), "method": "Efectivo"})(),
        ctx=default_context(org_b),
    )

    with pytest.raises(AppError) as exc:
        list_payments(session, charge_b.id, ctx=default_context(ORG))
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()


# --- permissions / audit / idempotency / PF gap ------------------------------


def test_economic_commands_enforce_permissions(session):
    ids = seed_booking(session)
    no_perm = seed_actor(session, codes=())
    ctx = ctx_for(no_perm)

    with pytest.raises(AppError) as exc:
        create_product(
            session,
            type("D", (), {"name": "X", "unit": "u", "kind": "consumible"})(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    product = make_product(session)
    product_id = product.id
    execution = make_execution(session, ids)
    execution_id = execution.id

    with pytest.raises(AppError) as exc:
        create_service_consumption(
            session,
            execution_id,
            type("D", (), {"product_id": product_id, "quantity": Decimal("1"), "unit_price": Decimal("1")})(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_charge(session, execution_id, type("D", (), {"amount": Decimal("1")})(), ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    charge = create_charge(
        session,
        execution_id,
        type("D", (), {"amount": Decimal("100")})(),
        ctx=default_context(ORG),
    )
    with pytest.raises(AppError) as exc:
        create_payment(
            session,
            charge.id,
            type("D", (), {"amount": Decimal("1"), "method": "Efectivo"})(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()


def test_audit_provenance_for_economic_creates(session):
    ids = seed_booking(session)
    actor = seed_actor(
        session,
        codes=(PRODUCTS_CREATE, CONSUMPTIONS_CREATE, CHARGES_CREATE, PAYMENTS_CREATE),
    )
    ctx = ctx_for(actor)

    product = create_product(
        session,
        type("D", (), {"name": "Anestesia", "unit": "ampolla", "kind": "consumible"})(),
        ctx=ctx,
    )
    product_id = product.id
    execution = make_execution(session, ids)
    execution_id = execution.id
    make_entry(session, product_id, Decimal("5.00"), location_id=ids["location_id"])
    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_id, "quantity": Decimal("1"), "unit_price": Decimal("1")})(),
        ctx=ctx,
    )
    charge = create_charge(
        session,
        execution_id,
        type("D", (), {"amount": Decimal("100")})(),
        ctx=ctx,
    )
    create_payment(
        session,
        charge.id,
        type("D", (), {"amount": Decimal("50"), "method": "Yape"})(),
        ctx=ctx,
    )

    rows = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    session.rollback()
    actions = [row.action for row in rows]
    economic = [a for a in actions if a in {
        "product.created",
        "service_consumption.created",
        "charge.created",
        "payment.created",
    }]
    assert economic == [
        "product.created",
        "service_consumption.created",
        "charge.created",
        "payment.created",
    ]
    assert all(row.actor_id == str(actor) for row in rows if row.action in economic)


def test_economic_creates_are_idempotent(session):
    ids = seed_booking(session)
    ctx = default_context(ORG)

    product_payload = type("D", (), {"name": "Anestesia", "unit": "ampolla", "kind": "consumible"})()
    p1 = run_idempotent_command(
        session,
        operation=create_product,
        operation_name=OP_PRODUCTS_CREATE,
        key="econ-p-1",
        ctx=ctx,
        params={"name": "Anestesia", "unit": "ampolla", "kind": "consumible"},
        data=product_payload,
    )
    p2 = run_idempotent_command(
        session,
        operation=create_product,
        operation_name=OP_PRODUCTS_CREATE,
        key="econ-p-1",
        ctx=ctx,
        params={"name": "Anestesia", "unit": "ampolla", "kind": "consumible"},
        data=product_payload,
    )
    product_id = p1.result.id  # capture immediately (the replay rollback expires it)
    assert p2.replayed is True
    assert p2.outcome["resource_id"] == str(product_id)
    session.rollback()  # the replay read left a transaction open

    make_entry(session, product_id, Decimal("5.00"), location_id=ids["location_id"])

    execution = make_execution(session, ids)
    execution_id = execution.id
    consumption_payload = type(
        "D", (), {"product_id": product_id, "quantity": Decimal("1"), "unit_price": Decimal("1")}
    )()
    c_params = {"service_execution_id": execution_id, "product_id": product_id,
                "quantity": "1", "unit_price": "1"}
    c1 = run_idempotent_command(
        session,
        operation=create_service_consumption,
        operation_name=OP_CONSUMPTIONS_CREATE,
        key="econ-c-1",
        ctx=ctx,
        params=c_params,
        execution_id=execution_id,
        data=consumption_payload,
    )
    c2 = run_idempotent_command(
        session,
        operation=create_service_consumption,
        operation_name=OP_CONSUMPTIONS_CREATE,
        key="econ-c-1",
        ctx=ctx,
        params=c_params,
        execution_id=execution_id,
        data=consumption_payload,
    )
    assert c2.replayed is True
    assert c2.outcome["resource_id"] == str(c1.result.id)
    session.rollback()  # the replay read left a transaction open

    charge_payload = type("D", (), {"amount": Decimal("100")})()
    ch1 = run_idempotent_command(
        session,
        operation=create_charge,
        operation_name=OP_CHARGES_CREATE,
        key="econ-ch-1",
        ctx=ctx,
        params={"service_execution_id": execution_id, "amount": "100"},
        execution_id=execution_id,
        data=charge_payload,
    )
    ch2 = run_idempotent_command(
        session,
        operation=create_charge,
        operation_name=OP_CHARGES_CREATE,
        key="econ-ch-1",
        ctx=ctx,
        params={"service_execution_id": execution_id, "amount": "100"},
        execution_id=execution_id,
        data=charge_payload,
    )
    charge_id = ch1.result.id  # capture immediately (the replay rollback expires it)
    assert ch2.replayed is True
    assert ch2.outcome["resource_id"] == str(charge_id)
    session.rollback()  # the replay read left a transaction open

    payment_payload = type("D", (), {"amount": Decimal("50"), "method": "Yape"})()
    pay1 = run_idempotent_command(
        session,
        operation=create_payment,
        operation_name=OP_PAYMENTS_CREATE,
        key="econ-pay-1",
        ctx=ctx,
        params={"charge_id": charge_id, "amount": "50", "method": "Yape"},
        charge_id=charge_id,
        data=payment_payload,
    )
    pay2 = run_idempotent_command(
        session,
        operation=create_payment,
        operation_name=OP_PAYMENTS_CREATE,
        key="econ-pay-1",
        ctx=ctx,
        params={"charge_id": charge_id, "amount": "50", "method": "Yape"},
        charge_id=charge_id,
        data=payment_payload,
    )
    assert pay2.replayed is True
    assert pay2.outcome["resource_id"] == str(pay1.result.id)
    session.rollback()  # the replay read left a transaction open

    assert session.scalar(select(func.count()).select_from(Product)) == 1
    assert session.scalar(select(func.count()).select_from(ServiceConsumption)) == 1
    assert session.scalar(select(func.count()).select_from(Charge)) == 1
    assert session.scalar(select(func.count()).select_from(Payment)) == 1
    # One receipt per idempotency key (replays reuse the stored receipt).
    assert session.scalar(select(func.count()).select_from(CommandReceipt)) == 4
    session.rollback()


def test_runtime_organization_gets_system_access_atomically(session):
    """PF gap fix: create_organization provisions system access in the same
    transaction — a new org is immediately operable by the platform actor."""
    org_b = create_organization(session, "Otra Clínica").id
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")

    # The system principal can act in the runtime org without extra wiring.
    product = create_product(
        session,
        type("D", (), {"name": "Anestesia", "unit": "ampolla", "kind": "consumible"})(),
        ctx=default_context(org_b),
    )
    assert product.organization_id == org_b


# --- HTTP --------------------------------------------------------------------


@pytest.fixture
def api_app(migrated_engine):
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
    return app, maker


@pytest.fixture
def client(api_app):
    app, _ = api_app
    return TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)


def test_economic_http_journey(client, session):
    ids = seed_booking(session)
    product = client.post(
        "/products",
        json={"name": "Anestesia", "unit": "ampolla", "kind": "consumible"},
        headers={"Idempotency-Key": "http-prod-1"},
    )
    assert product.status_code == 201, product.text
    product_id = product.json()["id"]

    replay = client.post(
        "/products",
        json={"name": "Anestesia", "unit": "ampolla", "kind": "consumible"},
        headers={"Idempotency-Key": "http-prod-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == product_id
    assert replay.headers.get("Idempotent-Replay") == "true"

    assert client.get("/products").status_code == 200
    assert len(client.get("/products", params={"kind": "consumible"}).json()) == 1

    execution = make_execution(session, ids)
    execution_id = execution.id

    client.post(
        f"/products/{product_id}/entries",
        json={"quantity": "10.00", "location_id": ids["location_id"]},
        headers={"Idempotency-Key": "http-entry-1"},
    )
    consumption = client.post(
        f"/executions/{execution_id}/consumptions",
        json={"product_id": product_id, "quantity": "2.00", "unit_price": "25.50"},
        headers={"Idempotency-Key": "http-cons-1"},
    )
    assert consumption.status_code == 201, consumption.text
    assert consumption.json()["amount"] == "51.00"

    charge = client.post(
        f"/executions/{execution_id}/charges",
        json={},
        headers={"Idempotency-Key": "http-charge-1"},
    )
    assert charge.status_code == 201, charge.text
    charge_id = charge.json()["id"]
    assert charge.json()["amount"] == "150.00"  # execution price snapshot

    payment = client.post(
        f"/charges/{charge_id}/payments",
        json={"amount": "150.00", "method": "Efectivo"},
        headers={"Idempotency-Key": "http-pay-1"},
    )
    assert payment.status_code == 201, payment.text

    detail = client.get(f"/charges/{charge_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["paid"] == "150.00"
    assert body["outstanding"] == "0.00"

    overpay = client.post(
        f"/charges/{charge_id}/payments",
        json={"amount": "1.00", "method": "Efectivo"},
    )
    assert overpay.status_code == 422
    assert overpay.json()["error"]["code"] == "INVALID_INPUT"

    assert len(client.get(f"/charges/{charge_id}/payments").json()) == 1


# --- repair-pass proofs (review ISSUEs) -------------------------------------


def test_concurrent_duplicate_product_name_settles_as_422(migrated_engine, session):
    """Two different idempotency keys, same org, same product name, racing:
    the unique index settles it and the loser gets a stable 422 — never a
    raw 23505/500."""
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt(key):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            create_product(
                db,
                type("D", (), {"name": "Anestesia", "unit": "ampolla", "kind": "consumible"})(),
                ctx=default_context(ORG),
            )
            with guard:
                outcomes.append(("committed", key))
        except AppError as exc:
            db.rollback()
            with guard:
                outcomes.append(("app_error", exc.code, key))
        finally:
            db.close()

    threads = [
        threading.Thread(target=attempt, args=("prod-a",)),
        threading.Thread(target=attempt, args=("prod-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    committed = [o for o in outcomes if o[0] == "committed"]
    rejected = [o for o in outcomes if o[0] == "app_error"]
    assert len(committed) == 1, outcomes
    assert len(rejected) == 1, outcomes
    assert rejected[0][1] == ErrorCode.INVALID_INPUT, outcomes

    db = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)()
    rows = list_products(db, ctx=default_context(ORG))
    db.close()
    assert len(rows) == 1


def test_payments_are_append_only_at_the_orm_level(session):
    """The Charge.payments relationship has no delete-orphan cascade: ORM
    deletion of a charge cannot silently erase payment history."""
    ids = seed_booking(session)
    charge = create_charge(
        session,
        make_execution(session, ids).id,
        type("D", (), {"amount": Decimal("100")})(),
        ctx=default_context(ORG),
    )
    create_payment(
        session,
        charge.id,
        type("D", (), {"amount": Decimal("50"), "method": "Efectivo"})(),
        ctx=default_context(ORG),
    )
    from app.economics.models import Charge as ChargeModel

    mapper = ChargeModel.__mapper__
    relationship = mapper.relationships["payments"]
    assert relationship.cascade.delete_orphan is False
    assert relationship.cascade.delete is False
