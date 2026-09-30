"""B0.5 §3 — reorder points, low-stock read and the below-reorder crossing event."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.clinical.schemas import PatientCreate, ServiceExecutionCreate, VisitCreate
from app.clinical.service import create_patient, create_service_execution, create_visit
from app.context import default_context
from app.economics.schemas import ServiceConsumptionCreate
from app.economics.service import create_service_consumption
from app.inventory.models import ReorderPoint
from app.inventory.schemas import AdjustmentCreate, ReorderPointUpsert, TransferCreate
from app.inventory.service import (
    list_low_stock,
    register_adjustment,
    transfer_product,
    upsert_reorder_point,
)
from app.organization.service import create_organization
from test_domain_gaps_helpers import (  # noqa: F401  (``api`` is a fixture)
    ORG,
    api,
    domain_events,
    make_location,
    make_product,
    seed_booking,
    stock,
)

EVENT = "inventory.below_reorder"


def _set_min(session, product_id, location_id, minimum, *, organization_id=ORG):
    return upsert_reorder_point(
        session,
        product_id,
        location_id,
        ReorderPointUpsert(min_quantity=Decimal(minimum)),
        ctx=default_context(organization_id),
    )


def _adjust(session, product_id, location_id, quantity):
    return register_adjustment(
        session,
        product_id,
        AdjustmentCreate(location_id=location_id, quantity=Decimal(quantity), reason="Conteo"),
        ctx=default_context(ORG),
    )


def test_put_reorder_point_is_an_idempotent_upsert(api, session):
    product_id = make_product(session)
    location_id = make_location(session)

    first = api.put(
        f"/products/{product_id}/reorder-points/{location_id}", json={"min_quantity": "10"}
    )
    second = api.put(
        f"/products/{product_id}/reorder-points/{location_id}",
        json={"min_quantity": "12.5"},
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"]
    assert Decimal(second.json()["min_quantity"]) == Decimal("12.5")
    count = session.scalar(select(func.count()).select_from(ReorderPoint))
    session.rollback()
    assert count == 1


def test_negative_minimum_is_rejected(api, session):
    product_id = make_product(session)
    location_id = make_location(session)

    response = api.put(
        f"/products/{product_id}/reorder-points/{location_id}", json={"min_quantity": "-1"}
    )

    assert response.status_code == 422
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO reorder_points (organization_id, product_id, location_id, min_quantity) "
                "VALUES (:org, :product, :location, -1)"
            ),
            {"org": ORG, "product": product_id, "location": location_id},
        )
        session.flush()
    session.rollback()


def test_low_stock_lists_only_rows_below_the_minimum(api, session):
    product_id = make_product(session, name="Anestesia")
    other_id = make_product(session, name="Guantes")
    low_site = make_location(session, name="Sede Baja")
    ample_site = make_location(session, name="Sede Holgada")
    stock(session, product_id, low_site, "4")
    stock(session, product_id, ample_site, "120")
    stock(session, other_id, low_site, "10")
    _set_min(session, product_id, low_site, "10")
    _set_min(session, product_id, ample_site, "10")
    _set_min(session, other_id, low_site, "10")  # balance == min is not low

    everything = api.get("/inventory/low-stock")
    only_low_site = api.get("/inventory/low-stock", params={"location_id": low_site})
    only_ample_site = api.get("/inventory/low-stock", params={"location_id": ample_site})

    assert everything.status_code == 200, everything.text
    rows = everything.json()
    assert [(row["product_id"], row["location_id"]) for row in rows] == [(product_id, low_site)]
    assert Decimal(rows[0]["balance"]) == Decimal("4")
    assert Decimal(rows[0]["min_quantity"]) == Decimal("10")
    assert rows[0]["product_name"] == "Anestesia"
    assert rows[0]["location_name"] == "Sede Baja"
    assert len(only_low_site.json()) == 1
    assert only_ample_site.json() == []
    assert list_low_stock(session, ctx=default_context(ORG))[0].product_id == product_id


def test_a_product_without_movements_below_its_minimum_is_low(api, session):
    product_id = make_product(session)
    location_id = make_location(session)
    _set_min(session, product_id, location_id, "5")

    rows = api.get("/inventory/low-stock").json()

    assert [(row["product_id"], Decimal(row["balance"])) for row in rows] == [
        (product_id, Decimal("0"))
    ]


def test_adjustment_crossing_below_the_minimum_emits_once(session):
    product_id = make_product(session)
    location_id = make_location(session)
    stock(session, product_id, location_id, "12")
    _set_min(session, product_id, location_id, "10")

    _adjust(session, product_id, location_id, "-1")  # 12 → 11: still above
    assert domain_events(session, EVENT) == []
    _adjust(session, product_id, location_id, "-3")  # 11 → 8: crosses
    _adjust(session, product_id, location_id, "-2")  # 8 → 6: already below

    events = domain_events(session, EVENT)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["product_id"] == product_id
    assert payload["location_id"] == location_id
    assert Decimal(str(payload["balance_before"])) == Decimal("11")
    assert Decimal(str(payload["balance_after"])) == Decimal("8")
    assert Decimal(str(payload["min_quantity"])) == Decimal("10")


def test_going_back_above_and_crossing_again_emits_again(session):
    product_id = make_product(session)
    location_id = make_location(session)
    stock(session, product_id, location_id, "11")
    _set_min(session, product_id, location_id, "10")

    _adjust(session, product_id, location_id, "-2")  # 11 → 9: cross
    stock(session, product_id, location_id, "5")  # 9 → 14: back above
    _adjust(session, product_id, location_id, "-5")  # 14 → 9: cross again

    assert len(domain_events(session, EVENT)) == 2


def test_no_reorder_point_means_no_event(session):
    product_id = make_product(session)
    location_id = make_location(session)
    stock(session, product_id, location_id, "3")

    _adjust(session, product_id, location_id, "-2")

    assert domain_events(session, EVENT) == []


def test_setting_a_minimum_above_the_balance_does_not_emit(session):
    product_id = make_product(session)
    location_id = make_location(session)
    stock(session, product_id, location_id, "3")

    _set_min(session, product_id, location_id, "10")

    assert domain_events(session, EVENT) == []


def test_transfer_out_crossing_emits_for_the_origin_only(session):
    product_id = make_product(session)
    origin = make_location(session, name="Origen")
    destination = make_location(session, name="Destino")
    stock(session, product_id, origin, "15")
    _set_min(session, product_id, origin, "10")
    _set_min(session, product_id, destination, "10")

    transfer_product(
        session,
        product_id,
        TransferCreate(
            origin_location_id=origin, destination_location_id=destination, quantity=Decimal("8")
        ),
        ctx=default_context(ORG),
    )

    events = domain_events(session, EVENT)
    assert [event.payload["location_id"] for event in events] == [origin]


def test_consumption_crossing_emits(session):
    ids = seed_booking(session, suffix="cons")
    product_id = make_product(session)
    stock(session, product_id, ids["location_id"], "10")
    _set_min(session, product_id, ids["location_id"], "10")
    ctx = default_context(ORG)
    patient = create_patient(session, PatientCreate(full_name="Paciente Consumo", dni="71000055"), ctx=ctx)
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
        ServiceExecutionCreate(service_id=ids["service_id"], executed_price=Decimal("150.00")),
        ctx=ctx,
    )

    create_service_consumption(
        session,
        execution.id,
        ServiceConsumptionCreate(product_id=product_id, quantity=Decimal("1"), unit_price=Decimal("0")),
        ctx=ctx,
    )

    events = domain_events(session, EVENT)
    assert len(events) == 1
    assert events[0].payload["location_id"] == ids["location_id"]


def test_reorder_points_are_tenant_isolated(api, session):
    org_b = create_organization(session, "Clínica Stock B").id
    session.commit()
    foreign_product = make_product(session, organization_id=org_b, name="Ajeno")
    foreign_location = make_location(session, organization_id=org_b, name="Sede Ajena")
    own_location = make_location(session)
    _set_min(session, foreign_product, foreign_location, "10", organization_id=org_b)

    put_foreign = api.put(
        f"/products/{foreign_product}/reorder-points/{own_location}", json={"min_quantity": "1"}
    )
    low = api.get("/inventory/low-stock").json()
    filtered = api.get("/inventory/low-stock", params={"location_id": foreign_location})

    assert put_foreign.status_code == 404, put_foreign.text
    assert low == []
    assert filtered.status_code == 404
    own_product = make_product(session, name="Propio")
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO reorder_points (organization_id, product_id, location_id, min_quantity) "
                "VALUES (:org, :product, :location, 1)"
            ),
            {"org": ORG, "product": own_product, "location": foreign_location},
        )
        session.flush()
    session.rollback()
