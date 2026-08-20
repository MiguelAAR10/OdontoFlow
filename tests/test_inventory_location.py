"""M4.2 — Location-aware inventory proofs against real PostgreSQL.

Extends the PF7 ledger from Product × Organization to Product × Location
WITHOUT introducing a second stock authority: ``inventory_movements`` keeps
being the only truth; the balance is still a derived read-time aggregate, now
per ``(organization_id, product_id, location_id)``.

Proves: entries/adjustments target a Location; balance and kardex are
Location-scoped; stock at other Locations is untouched; the clinical
consumption stock-out uses the Location of its Visit/ServiceExecution;
insufficient stock is rejected per Location; concurrent consumption/transfer
never overspends; transfers are one atomic stock-conserving pair
(TRANSFER_OUT → shared transfer_id → TRANSFER_IN) with DB-enforced pair
invariants, PF4 idempotency, audit and permissions; and the migration cycle.
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
from app.idempotency.service import run_idempotent_command
from app.inventory.models import (
    ADJUSTMENT,
    ENTRADA,
    SALIDA,
    TRANSFER_IN,
    TRANSFER_OUT,
    InventoryMovement,
)
from app.inventory.service import (
    OP_ENTRIES_CREATE,
    OP_TRANSFERS_CREATE,
    available_balance,
    get_balance,
    list_movements,
    register_adjustment,
    register_entry,
    transfer_product,
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
        request_id="req-invloc",
        correlation_id="corr-invloc",
    )


def make_location(session, *, organization_id=ORG, name="Sede A", timezone=LIMA):
    location = Location(
        organization_id=organization_id,
        name=name,
        timezone=timezone,
        is_active=True,
    )
    session.add(location)
    session.commit()
    return location.id


def make_product(session, *, organization_id=ORG, name="Anestesia", kind="consumible"):
    return create_product(
        session,
        type("D", (), {"name": name, "unit": "ampolla", "kind": kind})(),
        ctx=default_context(organization_id),
    )


def make_entry(session, product_id, quantity, *, location_id, organization_id=ORG):
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
        type("D", (), {"full_name": "Paciente Uno", "dni": dni or f"9{organization_id}000001",
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


def transfer_payload(product_id, origin, destination, quantity, reason=None):
    return type(
        "D",
        (),
        {
            "origin_location_id": origin,
            "destination_location_id": destination,
            "quantity": Decimal(str(quantity)),
            "reason": reason,
        },
    )()


# --- entries / adjustments / balance are Location-scoped ---------------------


def test_entries_target_a_location_and_balances_are_isolated(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")

    make_entry(session, product_id, "10.00", location_id=loc_a)
    make_entry(session, product_id, "5.00", location_id=loc_b)
    make_entry(session, product_id, "2.00", location_id=loc_a)

    assert get_balance(session, product_id, location_id=loc_a, ctx=default_context(ORG)) == Decimal("12.00")
    assert get_balance(session, product_id, location_id=loc_b, ctx=default_context(ORG)) == Decimal("5.00")
    session.rollback()

    # The kardex is per-location too: no row leaks across locations.
    rows_a = list_movements(session, product_id, location_id=loc_a, ctx=default_context(ORG))
    rows_b = list_movements(session, product_id, location_id=loc_b, ctx=default_context(ORG))
    assert len(rows_a) == 2
    assert len(rows_b) == 1
    assert all(row.type == ENTRADA for row in rows_a + rows_b)
    session.rollback()


def test_adjustments_target_a_location(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "10.00", location_id=loc_a)
    make_entry(session, product_id, "10.00", location_id=loc_b)

    register_adjustment(
        session,
        product_id,
        type("D", (), {"quantity": Decimal("-3.00"), "reason": "merma en A", "location_id": loc_a})(),
        ctx=default_context(ORG),
    )
    assert get_balance(session, product_id, location_id=loc_a, ctx=default_context(ORG)) == Decimal("7.00")
    assert get_balance(session, product_id, location_id=loc_b, ctx=default_context(ORG)) == Decimal("10.00")
    session.rollback()

    # Insufficient stock in A is rejected even when B has plenty.
    with pytest.raises(AppError) as exc:
        register_adjustment(
            session,
            product_id,
            type("D", (), {"quantity": Decimal("-100.00"), "reason": "merma", "location_id": loc_a})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()
    assert get_balance(session, product_id, location_id=loc_a, ctx=default_context(ORG)) == Decimal("7.00")
    session.rollback()


def test_cross_organization_location_rejected(session):
    product = make_product(session)
    product_id = product.id
    org_b = create_organization(session, "Otra Clínica").id
    loc_b = make_location(session, organization_id=org_b, name="Sede B")

    # Org A's ledger cannot reference org B's location (composite FK).
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity)"
                " VALUES (:o, :p, :l, 'ENTRADA', 1)"
            ),
            {"o": ORG, "p": product_id, "l": loc_b},
        )
        session.commit()
    session.rollback()
    assert "fk_inventory_movements_organization_location" in str(exc.value)

    # Reading the balance of another org's location → NOT_FOUND, never a leak.
    with pytest.raises(AppError) as exc:
        get_balance(session, product_id, location_id=loc_b, ctx=default_context(ORG))
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()


# --- clinical consumption uses the execution Location ------------------------


def test_consumption_reduces_stock_at_the_execution_location_only(session):
    ids = seed_booking(session)
    loc_visit = ids["location_id"]
    loc_other = make_location(session, name="Sede Otra")
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, "10.00", location_id=loc_visit)
    make_entry(session, product_id, "10.00", location_id=loc_other)
    execution = make_execution(session, ids)
    execution_id = execution.id

    create_service_consumption(
        session,
        execution_id,
        type("D", (), {"product_id": product_id, "quantity": Decimal("3.00"), "unit_price": Decimal("25.00")})(),
        ctx=default_context(ORG),
    )

    assert get_balance(session, product_id, location_id=loc_visit, ctx=default_context(ORG)) == Decimal("7.00")
    assert get_balance(session, product_id, location_id=loc_other, ctx=default_context(ORG)) == Decimal("10.00")
    session.rollback()

    # The SALIDA row itself is anchored to the visit location.
    salida = session.scalar(
        select(InventoryMovement).where(InventoryMovement.type == SALIDA)
    )
    assert salida is not None
    assert salida.location_id == loc_visit
    session.rollback()


def test_consumption_rejected_when_stock_insufficient_at_execution_location(session):
    ids = seed_booking(session)
    loc_other = make_location(session, name="Sede Otra")
    product = make_product(session)
    product_id = product.id
    make_entry(session, product_id, "10.00", location_id=loc_other)
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

    # Nothing was written: the other-location stock is untouched.
    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
    assert get_balance(session, product_id, location_id=loc_other, ctx=default_context(ORG)) == Decimal("10.00")
    session.rollback()


def test_concurrent_consumption_does_not_overspend_at_one_location(migrated_engine, session):
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
        threading.Thread(target=attempt, args=("loc-cons-a",)),
        threading.Thread(target=attempt, args=("loc-cons-b",)),
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


# --- transfers ---------------------------------------------------------------


def test_transfer_moves_stock_between_locations_atomically(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "10.00", location_id=loc_a)

    result = transfer_product(
        session,
        product_id,
        transfer_payload(product_id, loc_a, loc_b, "4.00", reason="reabastecer B"),
        ctx=default_context(ORG),
    )

    assert result.origin_location_id == loc_a
    assert result.destination_location_id == loc_b
    assert result.quantity == Decimal("4.00")
    assert result.out_movement_id != result.in_movement_id
    session.rollback()

    # Stock-conserving: A loses 4, B gains 4, total is unchanged.
    assert get_balance(session, product_id, location_id=loc_a, ctx=default_context(ORG)) == Decimal("6.00")
    assert get_balance(session, product_id, location_id=loc_b, ctx=default_context(ORG)) == Decimal("4.00")
    session.rollback()

    total = session.execute(
        select(InventoryMovement.type, InventoryMovement.quantity).where(
            InventoryMovement.organization_id == ORG,
            InventoryMovement.product_id == product_id,
        )
    ).all()
    available = Decimal("0")
    for movement_type, quantity in total:
        if movement_type in (ENTRADA, TRANSFER_IN):
            available += quantity
        else:  # SALIDA / ADJUSTMENT(signed) / TRANSFER_OUT
            available -= quantity
    assert available == Decimal("10.00")
    session.rollback()

    # The pair shares one transfer identity and the ledger vocabulary.
    rows = list_movements(session, product_id, location_id=loc_a, ctx=default_context(ORG))
    out_row = [row for row in rows if row.type == TRANSFER_OUT]
    assert len(out_row) == 1
    in_rows = list_movements(session, product_id, location_id=loc_b, ctx=default_context(ORG))
    in_row = [row for row in in_rows if row.type == TRANSFER_IN]
    assert len(in_row) == 1
    assert out_row[0].transfer_id == in_row[0].transfer_id
    assert out_row[0].quantity == in_row[0].quantity == Decimal("4.00")
    session.rollback()


def test_transfer_rejected_without_sufficient_origin_stock(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "3.00", location_id=loc_a)

    with pytest.raises(AppError) as exc:
        transfer_product(
            session,
            product_id,
            transfer_payload(product_id, loc_a, loc_b, "4.00"),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # Nothing was written: no movements, no partial pair.
    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
    assert get_balance(session, product_id, location_id=loc_a, ctx=default_context(ORG)) == Decimal("3.00")
    assert get_balance(session, product_id, location_id=loc_b, ctx=default_context(ORG)) == Decimal("0.00")
    session.rollback()


def test_transfer_rejected_between_the_same_location(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    make_entry(session, product_id, "10.00", location_id=loc_a)

    with pytest.raises(AppError) as exc:
        transfer_product(
            session,
            product_id,
            transfer_payload(product_id, loc_a, loc_a, "1.00"),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()


def test_transfer_pair_invariants_are_structural(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")

    # A TRANSFER_OUT without a transfer_id is rejected.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity)"
                " VALUES (:o, :p, :l, 'TRANSFER_OUT', 1)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a},
        )
        session.commit()
    session.rollback()
    assert "requires a transfer_id" in str(exc.value)

    # A transfer_id on a non-transfer movement is rejected.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'ENTRADA', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "t": "x" * 36},
        )
        session.commit()
    session.rollback()
    assert "only valid on TRANSFER movements" in str(exc.value)

    # An orphan TRANSFER_OUT (no paired TRANSFER_IN) is rejected at commit.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'TRANSFER_OUT', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "t": "a" * 36},
        )
        session.commit()
    session.rollback()
    assert "requires a paired TRANSFER_IN" in str(exc.value)

    # A mismatched pair (different quantity) is rejected at commit.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'TRANSFER_OUT', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "t": "b" * 36},
        )
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l2, 'TRANSFER_IN', 2, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "l2": loc_b, "t": "b" * 36},
        )
        session.commit()
    session.rollback()
    assert "share organization, product and quantity" in str(exc.value)

    # A pair that moves between the same location is rejected at commit.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'TRANSFER_OUT', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "t": "c" * 36},
        )
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'TRANSFER_IN', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "t": "c" * 36},
        )
        session.commit()
    session.rollback()
    assert "distinct locations" in str(exc.value)

    # Exactly one OUT and one IN per transfer_id: a second OUT is impossible.
    out_id = "d" * 36
    session.execute(
        text(
            "INSERT INTO inventory_movements"
            " (organization_id, product_id, location_id, type, quantity, transfer_id)"
            " VALUES (:o, :p, :l, 'TRANSFER_OUT', 1, :t)"
        ),
        {"o": ORG, "p": product_id, "l": loc_a, "t": out_id},
    )
    session.execute(
        text(
            "INSERT INTO inventory_movements"
            " (organization_id, product_id, location_id, type, quantity, transfer_id)"
            " VALUES (:o, :p, :l2, 'TRANSFER_IN', 1, :t)"
        ),
        {"o": ORG, "p": product_id, "l": loc_a, "l2": loc_b, "t": out_id},
    )
    session.commit()
    session.rollback()
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'TRANSFER_OUT', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_a, "t": out_id},
        )
        session.commit()
    session.rollback()
    assert "uq_inventory_movements_transfer_out" in str(exc.value)


def test_concurrent_transfers_never_overdraw_the_origin(migrated_engine, session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "5.00", location_id=loc_a)

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
            transfer_product(
                db,
                product_id,
                transfer_payload(product_id, loc_a, loc_b, "4.00"),
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
        threading.Thread(target=attempt, args=("tr-a",)),
        threading.Thread(target=attempt, args=("tr-b",)),
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
    balance_a = available_balance(db, product_id, ORG, loc_a)
    balance_b = available_balance(db, product_id, ORG, loc_b)
    db.close()
    assert balance_a == Decimal("1.00")
    assert balance_b == Decimal("4.00")
    assert balance_a + balance_b == Decimal("5.00")


def test_transfer_is_idempotent(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "10.00", location_id=loc_a)
    ctx = default_context(ORG)
    payload = transfer_payload(product_id, loc_a, loc_b, "4.00")

    first = run_idempotent_command(
        session,
        operation=transfer_product,
        operation_name=OP_TRANSFERS_CREATE,
        key="inv-transfer-1",
        ctx=ctx,
        params={
            "product_id": product_id,
            "origin_location_id": loc_a,
            "destination_location_id": loc_b,
            "quantity": "4.00",
            "reason": None,
        },
        product_id=product_id,
        data=payload,
    )
    transfer_id = first.result.transfer_id
    second = run_idempotent_command(
        session,
        operation=transfer_product,
        operation_name=OP_TRANSFERS_CREATE,
        key="inv-transfer-1",
        ctx=ctx,
        params={
            "product_id": product_id,
            "origin_location_id": loc_a,
            "destination_location_id": loc_b,
            "quantity": "4.00",
            "reason": None,
        },
        product_id=product_id,
        data=payload,
    )
    assert second.replayed is True
    assert second.outcome["transfer_id"] == transfer_id
    session.rollback()

    # Exactly two movement rows: the stock moved exactly once.
    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 3  # entrada + pair
    assert get_balance(session, product_id, location_id=loc_a, ctx=ctx) == Decimal("6.00")
    assert get_balance(session, product_id, location_id=loc_b, ctx=ctx) == Decimal("4.00")
    session.rollback()


def test_transfer_is_audited(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "10.00", location_id=loc_a)
    actor = seed_actor(session, codes=(MOVEMENTS_CREATE,))
    ctx = ctx_for(actor)

    transfer_product(
        session,
        product_id,
        transfer_payload(product_id, loc_a, loc_b, "4.00"),
        ctx=ctx,
    )

    rows = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    session.rollback()
    actions = [row.action for row in rows]
    assert actions[-1] == "inventory_transfer.created"
    assert rows[-1].actor_id == str(actor)
    assert rows[-1].entity_type == "inventory_transfer"
    assert rows[-1].entity_id is not None


def test_transfer_requires_movements_create_permission(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")
    make_entry(session, product_id, "10.00", location_id=loc_a)
    no_perm = seed_actor(session, codes=())
    ctx = ctx_for(no_perm)

    with pytest.raises(AppError) as exc:
        transfer_product(
            session,
            product_id,
            transfer_payload(product_id, loc_a, loc_b, "1.00"),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()
    assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
    session.rollback()


def test_cross_organization_transfer_rejected(session):
    product = make_product(session)
    product_id = product.id
    org_b = create_organization(session, "Otra Clínica").id
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, organization_id=org_b, name="Sede B")
    make_entry(session, product_id, "10.00", location_id=loc_a)

    # The destination belongs to another org → NOT_FOUND at pre-validation.
    with pytest.raises(AppError) as exc:
        transfer_product(
            session,
            product_id,
            transfer_payload(product_id, loc_a, loc_b, "1.00"),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()

    # And the composite FK makes a cross-org pair structurally impossible.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (:o, :p, :l, 'TRANSFER_IN', 1, :t)"
            ),
            {"o": ORG, "p": product_id, "l": loc_b, "t": "e" * 36},
        )
        session.commit()
    session.rollback()
    assert "fk_inventory_movements_organization_location" in str(exc.value)


# --- structure ---------------------------------------------------------------


def test_location_id_is_not_null_and_tenant_qualified(session):
    product = make_product(session)
    product_id = product.id
    loc_a = make_location(session, name="Sede A")

    columns = {
        (row[0], row[1])
        for row in session.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_name = 'inventory_movements'"
                " AND column_name = 'location_id'"
            )
        )
    }
    session.rollback()
    assert columns == {("location_id", "NO")}

    constraints = {
        row[0]: row[1]
        for row in session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'inventory_movements'::regclass AND contype = 'f'"
            )
        )
    }
    session.rollback()
    assert (
        constraints["fk_inventory_movements_organization_location"]
        == "FOREIGN KEY (organization_id, location_id) REFERENCES locations(organization_id, id) ON DELETE RESTRICT"
    )
    # The location FK is composite: a product row must be in the same org too.
    assert (
        constraints["fk_inventory_movements_organization_product"]
        == "FOREIGN KEY (organization_id, product_id) REFERENCES products(organization_id, id) ON DELETE RESTRICT"
    )


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


def test_inventory_http_location_journey(client, session):
    loc_a = make_location(session, name="Sede A")
    loc_b = make_location(session, name="Sede B")

    product = client.post(
        "/products",
        json={"name": "Anestesia", "unit": "ampolla", "kind": "consumible"},
        headers={"Idempotency-Key": "invloc-http-p1"},
    )
    product_id = product.json()["id"]

    entry = client.post(
        f"/products/{product_id}/entries",
        json={"quantity": "10.00", "unit_price": "20.00", "location_id": loc_a},
        headers={"Idempotency-Key": "invloc-http-e1"},
    )
    assert entry.status_code == 201, entry.text
    assert entry.json()["location_id"] == loc_a

    balance_a = client.get(f"/products/{product_id}/balance", params={"location_id": loc_a})
    assert balance_a.status_code == 200
    assert balance_a.json()["available"] == "10.00"
    assert balance_a.json()["location_id"] == loc_a
    balance_b = client.get(f"/products/{product_id}/balance", params={"location_id": loc_b})
    assert Decimal(balance_b.json()["available"]) == Decimal("0.00")

    adjustment = client.post(
        f"/products/{product_id}/adjustments",
        json={"quantity": "-2.00", "reason": "merma", "location_id": loc_a},
        headers={"Idempotency-Key": "invloc-http-a1"},
    )
    assert adjustment.status_code == 201, adjustment.text
    assert adjustment.json()["location_id"] == loc_a

    movements_a = client.get(f"/products/{product_id}/movements", params={"location_id": loc_a})
    assert len(movements_a.json()) == 2
    movements_b = client.get(f"/products/{product_id}/movements", params={"location_id": loc_b})
    assert movements_b.json() == []

    transfer = client.post(
        f"/products/{product_id}/transfers",
        json={
            "origin_location_id": loc_a,
            "destination_location_id": loc_b,
            "quantity": "3.00",
            "reason": "reabastecer B",
        },
        headers={"Idempotency-Key": "invloc-http-t1"},
    )
    assert transfer.status_code == 201, transfer.text
    body = transfer.json()
    assert body["origin_location_id"] == loc_a
    assert body["destination_location_id"] == loc_b
    assert body["quantity"] == "3.00"
    assert body["out_movement_id"] != body["in_movement_id"]

    replay = client.post(
        f"/products/{product_id}/transfers",
        json={
            "origin_location_id": loc_a,
            "destination_location_id": loc_b,
            "quantity": "3.00",
            "reason": "reabastecer B",
        },
        headers={"Idempotency-Key": "invloc-http-t1"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["transfer_id"] == body["transfer_id"]
    assert replay.headers.get("Idempotent-Replay") == "true"

    balance_a = client.get(f"/products/{product_id}/balance", params={"location_id": loc_a})
    assert balance_a.json()["available"] == "5.00"  # 10 - 2 - 3
    balance_b = client.get(f"/products/{product_id}/balance", params={"location_id": loc_b})
    assert balance_b.json()["available"] == "3.00"

    missing_location = client.get(f"/products/{product_id}/balance")
    assert missing_location.status_code == 422
