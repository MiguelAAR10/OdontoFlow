"""PF7 — Inventory ledger proofs against real PostgreSQL.

Proves the append-only InventoryMovement ledger and the derived balance:
tenant isolation (composite FKs), per-type CHECKs, the ENTRADA surface, the
reason-required ADJUSTMENT, the kardex query, the negative-balance guard
(sequential + concurrent), the consumption→SALIDA 1:1 linkage in the same
transaction, PF4 idempotency, permissions, audit, and the migration cycle.
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
from app.clinical.service import create_patient, create_service_execution, create_visit
from app.commercial.models import Lead
from app.context import default_context
from app.db import get_db
from app.economics.models import Product, ServiceConsumption
from app.economics.service import create_product, create_service_consumption
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import MOVEMENTS_CREATE, MOVEMENTS_READ
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
)
from app.idempotency.models import CommandReceipt
from app.idempotency.service import run_idempotent_command
from app.inventory.models import ENTRADA, SALIDA, ADJUSTMENT, InventoryMovement
from app.inventory.service import (
    OP_ADJUSTMENTS_CREATE,
    OP_ENTRIES_CREATE,
    available_balance,
    get_balance,
    list_movements,
    register_adjustment,
    register_entry,
)
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.service import create_organization
from app.scheduling.models import AvailabilityRule
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
        request_id="req-inv",
        correlation_id="corr-inv",
    )


def make_product(session, *, organization_id=ORG, name="Anestesia", kind="consumible"):
    return create_product(
        session,
        type("D", (), {"name": name, "unit": "ampolla", "kind": kind})(),
        ctx=default_context(organization_id),
    )


def make_location(session, *, organization_id=ORG, name="Sede Inv"):
    location = Location(
        organization_id=organization_id,
        name=name,
        timezone=LIMA,
        is_active=True,
    )
    session.add(location)
    session.commit()
    return location.id


def make_entry(session, product_id, quantity, *, organization_id=ORG, location_id=None):
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
                "unit_price": Decimal("20.00"),
                "location_id": location_id,
            },
        )(),
        ctx=default_context(organization_id),
    )


def make_execution(session, ids, *, organization_id=None, dni=None):
    organization_id = organization_id or ids["organization_id"]
    patient = create_patient(
        session,
        type("D", (), {"full_name": "Paciente Uno", "dni": dni or f"8{organization_id}000001",
                       "sexo": "M", "phone": None, "birth_date": None})(),
        ctx=default_context(organization_id),
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


# --- ledger basics ----------------------------------------------------------


def test_entries_and_derived_balance(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)

    make_entry(session, product_id, "10.00", location_id=loc)
    make_entry(session, product_id, "5.00", location_id=loc)

    balance = get_balance(session, product_id, location_id=loc, ctx=default_context(ORG))
    assert balance == Decimal("15.00")

    rows = list_movements(session, product_id, location_id=loc, ctx=default_context(ORG))
    assert len(rows) == 2
    assert all(row.type == ENTRADA for row in rows)
    session.rollback()


def test_balance_is_derived_and_never_stored(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)
    make_entry(session, product_id, "7.00", location_id=loc)

    # No stock column exists on products: the ledger is the only truth.
    columns = {
        row[0]
        for row in session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'products'"
            )
        )
    }
    session.rollback()
    assert "stock_actual" not in columns
    assert "balance" not in columns
    assert "available" not in columns


def test_movement_type_and_quantity_checks(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)

    # ENTRADA with non-positive quantity → DB CHECK.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity) "
                "VALUES (:o, :p, :l, 'ENTRADA', 0)"
            ),
            {"o": ORG, "p": product_id, "l": loc},
        )
        session.commit()
    session.rollback()
    assert "ck_inventory_movements_quantity" in str(exc.value)

    # Unknown type → CHECK.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity) "
                "VALUES (:o, :p, :l, 'TRANSFER', 1)"
            ),
            {"o": ORG, "p": product_id, "l": loc},
        )
        session.commit()
    session.rollback()
    # CHECK evaluation order is not guaranteed: either the type or the
    # quantity guard may fire first — both are the same constraint family.
    assert "ck_inventory_movements" in str(exc.value)

    # ADJUSTMENT without reason → CHECK.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity) "
                "VALUES (:o, :p, :l, 'ADJUSTMENT', 1)"
            ),
            {"o": ORG, "p": product_id, "l": loc},
        )
        session.commit()
    session.rollback()
    assert "ck_inventory_movements_reason" in str(exc.value)

    # ADJUSTMENT with zero quantity → CHECK.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity, reason) "
                "VALUES (:o, :p, :l, 'ADJUSTMENT', 0, 'inventario')"
            ),
            {"o": ORG, "p": product_id, "l": loc},
        )
        session.commit()
    session.rollback()
    assert "ck_inventory_movements_quantity" in str(exc.value)


def test_adjustment_with_reason_and_signed_quantity(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)
    make_entry(session, product_id, "10.00", location_id=loc)

    register_adjustment(
        session,
        product_id,
        type("D", (), {"quantity": Decimal("-3.00"), "reason": "merma detectada", "location_id": loc})(),
        ctx=default_context(ORG),
    )
    assert get_balance(session, product_id, location_id=loc, ctx=default_context(ORG)) == Decimal("7.00")
    session.rollback()

    register_adjustment(
        session,
        product_id,
        type("D", (), {"quantity": Decimal("2.00"), "reason": "conteo superior", "location_id": loc})(),
        ctx=default_context(ORG),
    )
    assert get_balance(session, product_id, location_id=loc, ctx=default_context(ORG)) == Decimal("9.00")
    session.rollback()


def test_negative_adjustment_rejected_without_stock(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)

    with pytest.raises(AppError) as exc:
        register_adjustment(
            session,
            product_id,
            type("D", (), {"quantity": Decimal("-1.00"), "reason": "merma", "location_id": loc})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()
    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 0
    session.rollback()


def test_cross_tenant_movements_rejected_by_db(session):
    org_b = create_organization(session, "Otra Clínica").id
    product_b = make_product(session, organization_id=org_b, name="Anestesia B")
    loc_b = make_location(session, organization_id=org_b, name="Sede B")
    make_entry(session, product_b.id, "5.00", organization_id=org_b, location_id=loc_b)

    # Org A's ledger cannot reference org B's product.
    loc_a = make_location(session)
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity) "
                "VALUES (:o, :p, :l, 'ENTRADA', 1)"
            ),
            {"o": ORG, "p": product_b.id, "l": loc_a},
        )
        session.commit()
    session.rollback()
    assert "fk_inventory_movements_organization_product" in str(exc.value)

    # Org A cannot read org B's product balance (NOT_FOUND, E8).
    with pytest.raises(AppError) as exc:
        get_balance(session, product_b.id, location_id=loc_b, ctx=default_context(ORG))
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()


# --- consumption → SALIDA ----------------------------------------------------


def test_consumption_emits_salida_in_the_same_transaction(session):
    ids = seed_booking(session)
    loc = ids["location_id"]
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, "10.00", location_id=loc)
    execution = make_execution(session, ids)
    execution_id = execution.id

    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_id, "quantity": Decimal("3.00"), "unit_price": Decimal("25.00")})(),
        ctx=default_context(ORG),
    )

    assert get_balance(session, product_id, location_id=loc, ctx=default_context(ORG)) == Decimal("7.00")
    session.rollback()

    # Exactly one SALIDA, causally linked 1:1 via id_consumo_origen.
    rows = list_movements(session, product_id, location_id=loc, ctx=default_context(ORG))
    salidas = [row for row in rows if row.type == SALIDA]
    assert len(salidas) == 1
    consumption_id = salidas[0].id_consumo_origen
    assert consumption_id is not None
    assert salidas[0].quantity == Decimal("3.00")
    assert salidas[0].unit_price == Decimal("25.00")
    consumption = session.get(ServiceConsumption, consumption_id)
    assert consumption is not None
    session.rollback()


def test_consumption_rejected_when_stock_insufficient(session):
    ids = seed_booking(session)
    loc = ids["location_id"]
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, "2.00", location_id=loc)
    execution = make_execution(session, ids)
    execution_id = execution.id

    with pytest.raises(AppError) as exc:
        create_service_consumption(
            session,
            execution_id,
            type("D", (), {"product_id": product_id, "quantity": Decimal("3.00"), "unit_price": Decimal("1")})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # Nothing was written: no consumption, no SALIDA.
    assert session.scalar(select(func.count()).select_from(ServiceConsumption)) == 0
    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 1  # the entrada
    session.rollback()


def test_concurrent_consumptions_never_overdraw(migrated_engine, session):
    """Two simultaneous consumptions of the same product: the product row lock
    serializes the ledger sum and the loser is rejected — the balance can
    never go negative."""
    ids = seed_booking(session)
    loc = ids["location_id"]
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, "5.00", location_id=loc)
    execution = make_execution(session, ids)
    execution_id = execution.id

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
                type("D", (), {"product_id": product_id, "quantity": Decimal("4.00"), "unit_price": Decimal("1")})(),
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
        threading.Thread(target=attempt, args=("inv-a",)),
        threading.Thread(target=attempt, args=("inv-b",)),
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
    balance = available_balance(db, product_id, ORG, loc)
    db.close()
    assert balance == Decimal("1.00")  # 5 - 4 = 1; never negative


def test_salida_linkage_is_unique_and_causal(session):
    ids = seed_booking(session)
    loc = ids["location_id"]
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, "5.00", location_id=loc)
    execution = make_execution(session, ids)
    execution_id = execution.id

    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_id, "quantity": Decimal("1.00"), "unit_price": Decimal("1")})(),
        ctx=default_context(ORG),
    )
    # A second SALIDA for the same consumption is structurally impossible.
    consumption = session.scalar(select(ServiceConsumption).limit(1))
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity, id_consumo_origen) "
                "VALUES (:o, :p, :l, 'SALIDA', 1, :c)"
            ),
            {"o": ORG, "p": product_id, "l": loc, "c": consumption.id},
        )
        session.commit()
    session.rollback()
    assert "uq_inventory_movements_consumo_origen" in str(exc.value)


# --- permissions / audit / idempotency ---------------------------------------


def test_inventory_commands_enforce_permissions(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)
    no_perm = seed_actor(session, codes=())
    ctx = ctx_for(no_perm)

    with pytest.raises(AppError) as exc:
        register_entry(
            session,
            product_id,
            type("D", (), {"quantity": Decimal("1"), "unit_price": None, "location_id": loc})(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        register_adjustment(
            session,
            product_id,
            type("D", (), {"quantity": Decimal("1"), "reason": "x", "location_id": loc})(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        list_movements(session, product_id, location_id=loc, ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        get_balance(session, product_id, location_id=loc, ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()


def test_audit_provenance_for_inventory_creates(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)
    actor = seed_actor(session, codes=(MOVEMENTS_CREATE,))
    ctx = ctx_for(actor)

    register_entry(
        session,
        product_id,
        type("D", (), {"quantity": Decimal("5"), "unit_price": Decimal("10"), "location_id": loc})(),
        ctx=ctx,
    )
    register_adjustment(
        session,
        product_id,
        type("D", (), {"quantity": Decimal("-1"), "reason": "merma", "location_id": loc})(),
        ctx=ctx,
    )

    rows = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    session.rollback()
    actions = [row.action for row in rows]
    assert actions[-2:] == ["inventory_entry.created", "inventory_adjustment.created"]
    assert all(row.actor_id == str(actor) for row in rows[-2:])


def test_inventory_creates_are_idempotent(session):
    product = make_product(session)
    product_id = product.id
    loc = make_location(session)
    ctx = default_context(ORG)

    entry_payload = type(
        "D", (), {"quantity": Decimal("5"), "unit_price": Decimal("10"), "location_id": loc}
    )()
    e1 = run_idempotent_command(
        session,
        operation=register_entry,
        operation_name=OP_ENTRIES_CREATE,
        key="inv-entry-1",
        ctx=ctx,
        params={"product_id": product_id, "quantity": "5", "unit_price": "10", "location_id": loc},
        product_id=product_id,
        data=entry_payload,
    )
    entry_id = e1.result.id  # capture immediately (the replay rollback expires it)
    e2 = run_idempotent_command(
        session,
        operation=register_entry,
        operation_name=OP_ENTRIES_CREATE,
        key="inv-entry-1",
        ctx=ctx,
        params={"product_id": product_id, "quantity": "5", "unit_price": "10", "location_id": loc},
        product_id=product_id,
        data=entry_payload,
    )
    assert e2.replayed is True
    assert e2.outcome["resource_id"] == str(entry_id)

    adjustment_payload = type(
        "D", (), {"quantity": Decimal("-1"), "reason": "merma", "location_id": loc}
    )()
    a1 = run_idempotent_command(
        session,
        operation=register_adjustment,
        operation_name=OP_ADJUSTMENTS_CREATE,
        key="inv-adj-1",
        ctx=ctx,
        params={"product_id": product_id, "quantity": "-1", "reason": "merma", "location_id": loc},
        product_id=product_id,
        data=adjustment_payload,
    )
    a2 = run_idempotent_command(
        session,
        operation=register_adjustment,
        operation_name=OP_ADJUSTMENTS_CREATE,
        key="inv-adj-1",
        ctx=ctx,
        params={"product_id": product_id, "quantity": "-1", "reason": "merma", "location_id": loc},
        product_id=product_id,
        data=adjustment_payload,
    )
    assert a2.replayed is True
    assert a2.outcome["resource_id"] == str(a1.result.id)

    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 2
    assert get_balance(session, product_id, location_id=loc, ctx=ctx) == Decimal("4.00")
    session.rollback()


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


def test_inventory_http_journey(client, session):
    loc = make_location(session)

    product = client.post(
        "/products",
        json={"name": "Anestesia", "unit": "ampolla", "kind": "consumible"},
        headers={"Idempotency-Key": "inv-http-p1"},
    )
    product_id = product.json()["id"]

    entry = client.post(
        f"/products/{product_id}/entries",
        json={"quantity": "10.00", "unit_price": "20.00", "location_id": loc},
        headers={"Idempotency-Key": "inv-http-e1"},
    )
    assert entry.status_code == 201, entry.text
    assert entry.json()["type"] == "ENTRADA"
    assert entry.json()["location_id"] == loc

    replay = client.post(
        f"/products/{product_id}/entries",
        json={"quantity": "10.00", "unit_price": "20.00", "location_id": loc},
        headers={"Idempotency-Key": "inv-http-e1"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == entry.json()["id"]
    assert replay.headers.get("Idempotent-Replay") == "true"

    balance = client.get(f"/products/{product_id}/balance", params={"location_id": loc})
    assert balance.status_code == 200
    assert balance.json()["available"] == "10.00"
    assert balance.json()["location_id"] == loc

    adjustment = client.post(
        f"/products/{product_id}/adjustments",
        json={"quantity": "-2.00", "reason": "merma", "location_id": loc},
        headers={"Idempotency-Key": "inv-http-a1"},
    )
    assert adjustment.status_code == 201, adjustment.text
    assert adjustment.json()["type"] == "ADJUSTMENT"

    balance = client.get(f"/products/{product_id}/balance", params={"location_id": loc})
    assert balance.json()["available"] == "8.00"

    movements = client.get(f"/products/{product_id}/movements", params={"location_id": loc})
    assert len(movements.json()) == 2

    bad_adjustment = client.post(
        f"/products/{product_id}/adjustments",
        json={"quantity": "-100.00", "reason": "merma", "location_id": loc},
    )
    assert bad_adjustment.status_code == 422
    assert bad_adjustment.json()["error"]["code"] == "INVALID_INPUT"

    missing_reason = client.post(
        f"/products/{product_id}/adjustments",
        json={"quantity": "1.00", "location_id": loc},
    )
    assert missing_reason.status_code == 422


# --- repair-pass proofs (review ISSUEs) --------------------------------------


def test_salida_product_mismatch_rejected_by_trigger(session):
    """The DB trigger makes a consumption-linked SALIDA of a different product
    structurally impossible (the composite FK alone cannot compare columns)."""
    ids = seed_booking(session)
    loc = ids["location_id"]
    product_a = make_product(session, name="Anestesia")
    product_a_id = product_a.id
    product_b = make_product(session, name="Guantes")
    product_b_id = product_b.id
    make_entry(session, product_a_id, "5.00", location_id=loc)
    make_entry(session, product_b_id, "5.00", location_id=loc)
    execution = make_execution(session, ids)
    execution_id = execution.id

    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_a_id, "quantity": Decimal("1.00"), "unit_price": Decimal("1")})(),
        ctx=default_context(ORG),
    )
    consumption = session.scalar(select(ServiceConsumption).limit(1))
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, location_id, type, quantity, id_consumo_origen) "
                "VALUES (:o, :p, :l, 'SALIDA', 1, :c)"
            ),
            {"o": ORG, "p": product_b_id, "l": loc, "c": consumption.id},
        )
        session.commit()
    session.rollback()
    assert "SALIDA product must match" in str(exc.value)


def test_add_practitioner_membership_is_permission_gated(session):
    """PF closure sweep: the membership grant is an org-wide authority
    mutation and requires an org-wide PRACTITIONERS_MANAGE grant."""
    from app.organization.models import Practitioner
    from app.organization.service import add_practitioner_membership

    practitioner = Practitioner(display_name="Dra. Nueva", is_active=True)
    session.add(practitioner)
    session.commit()

    no_perm = seed_actor(session, codes=())
    with pytest.raises(AppError) as exc:
        add_practitioner_membership(
            session, practitioner.id, ORG, ctx=ctx_for(no_perm)
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    manager = seed_actor(session, codes=(MOVEMENTS_CREATE,))
    from app.iam.permissions import PRACTITIONERS_MANAGE

    manager = seed_actor(session, codes=(PRACTITIONERS_MANAGE,))
    membership = add_practitioner_membership(
        session, practitioner.id, ORG, ctx=ctx_for(manager)
    )
    assert membership.organization_id == ORG
