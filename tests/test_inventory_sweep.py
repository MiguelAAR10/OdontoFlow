"""INV — inventory agent: low-stock sweep, transfer/entry proposals with evidence.

Spec: ``docs/superpowers/specs/2026-10-01-erp-inv.md``. Real PostgreSQL, one
pytest process. The sweep only *proposes*; a human with ``movements.create``
approves and the existing ledger commands execute as that human.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import (
    _approve,
    _carlos,
    _count,
    _credential,
    _decline,
    _lucia,
    _other_org,
)

from app import create_app
from app.context import default_context
from app.db import get_db
from app.economics.models import Product
from app.economics.schemas import ProductCreate
from app.economics.service import create_product
from app.iam.models import Principal
from app.iam.service import add_membership, create_principal
from app.inventory.models import (
    ENTRADA,
    TRANSFER_IN,
    TRANSFER_OUT,
    InventoryMovement,
)
from app.inventory.schemas import AdjustmentCreate, EntryCreate, ReorderPointUpsert
from app.inventory.service import (
    available_balance,
    register_adjustment,
    register_entry,
    upsert_reorder_point,
)
from app.organization.models import Location
from app.proposals.models import AgentProposal
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import _assign_profile, _resolve_principal

LIMA = ZoneInfo("America/Lima")
AIRY = "airy-inventario"
PROFILE = "inventory-agent"


@pytest.fixture
def client(migrated_engine):
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    app = create_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _enabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_INVENTARIO_ENABLED", raising=False)


# --- helpers ------------------------------------------------------------------


def _agent_runs():
    from app.agents_runtime.models import AgentRun

    return AgentRun


def _idle(session):
    session.rollback()
    return session


def _location(session, name, organization_id=ORG) -> int:
    row = Location(organization_id=organization_id, name=name, timezone="America/Lima",
                   is_active=True)
    session.add(row)
    session.commit()
    return row.id


def _product(session, name, organization_id=ORG) -> int:
    product = create_product(
        _idle(session), ProductCreate(name=name, unit="unidad", kind="consumible"),
        ctx=default_context(organization_id),
    )
    return product.id


def _stock(session, product_id, location_id, quantity, organization_id=ORG) -> None:
    register_entry(
        _idle(session), product_id,
        EntryCreate(location_id=location_id, quantity=Decimal(quantity)),
        organization_id=organization_id,
    )


def _adjust(session, product_id, location_id, quantity, organization_id=ORG) -> None:
    register_adjustment(
        _idle(session), product_id,
        AdjustmentCreate(location_id=location_id, quantity=Decimal(quantity), reason="conteo"),
        organization_id=organization_id,
    )


def _min(session, product_id, location_id, minimum, organization_id=ORG) -> None:
    upsert_reorder_point(
        _idle(session), product_id, location_id,
        ReorderPointUpsert(min_quantity=Decimal(minimum)), organization_id=organization_id,
    )


def _point(session, product_id, location_id, *, balance, minimum=None, organization_id=ORG):
    if Decimal(balance) > 0:
        _stock(session, product_id, location_id, balance, organization_id)
    if minimum is not None:
        _min(session, product_id, location_id, minimum, organization_id)


def _airy(session, organization_id=ORG) -> int:
    principal = _resolve_principal(
        session, organization_id=organization_id, name=AIRY, principal_type="agent"
    )
    _assign_profile(session, organization_id=organization_id, principal_id=principal.id,
                    profile=PROFILE)
    session.commit()
    return principal.id


def _airy_caller(session, organization_id=ORG):
    return _credential(session, name=AIRY, principal_type="agent", profile=PROFILE,
                       organization_id=organization_id)


def _run(client, headers, *, key=None, body=None, send_key=True):
    request_headers = dict(headers)
    if send_key:
        request_headers["Idempotency-Key"] = key or str(uuid4())
    return client.post(
        "/agent-runs", json=body if body is not None else {"agent_key": "inventario"},
        headers=request_headers,
    )


def _proposals(session, *where):
    session.expire_all()
    rows = session.scalars(select(AgentProposal).where(*where).order_by(AgentProposal.id)).all()
    session.rollback()
    return rows


def _movements(session, organization_id=ORG):
    session.expire_all()
    rows = session.scalars(
        select(InventoryMovement)
        .where(InventoryMovement.organization_id == organization_id)
        .order_by(InventoryMovement.id)
    ).all()
    session.rollback()
    return rows


def _balance(session, product_id, location_id, organization_id=ORG) -> Decimal:
    value = available_balance(session, product_id, organization_id, location_id)
    session.rollback()
    return value


def _item(client, headers, proposal_id):
    response = client.get(f"/agent/proposals/{proposal_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _counts(**values):
    return {"candidates": 0, "proposed": 0, "deduped": 0, "skipped": 0, **values}


def _simple(session):
    """One product: A short (4 < 10), B donor (120, min 10)."""
    a = _location(session, "Sede A")
    b = _location(session, "Sede B")
    p = _product(session, "Anestesia")
    _point(session, p, a, balance="4", minimum="10")
    _point(session, p, b, balance="120", minimum="10")
    return {"a": a, "b": b, "p": p}


# --- 1/4. demo seed end to end ---------------------------------------------------


def test_demo_seed_run_proposes_the_lince_transfer_and_carlos_executes_it(client, session):
    from scripts.seed_demo import JESUS_MARIA, LINCE, LOW_STOCK_PRODUCT, seed_demo

    seed_demo(session, organization_id=ORG, anchor=datetime.now(LIMA).date())
    airy_id = session.scalar(select(Principal.id).where(Principal.display_name == AIRY))
    session.rollback()
    assert airy_id is not None  # the seed provisions the proposer
    product_id = session.scalar(
        select(Product.id).where(Product.organization_id == ORG, Product.name == LOW_STOCK_PRODUCT)
    )
    locations = {
        row.name: row.id
        for row in session.scalars(select(Location).where(Location.organization_id == ORG))
    }
    session.rollback()
    lince, jm = locations[LINCE], locations[JESUS_MARIA]
    _carlos_id, carlos = _carlos(session)

    first = _run(client, carlos, send_key=False)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["agent_key"] == "inventario" and body["status"] == "completed"
    assert body["counts"] == _counts(candidates=1, proposed=1)

    [proposal] = _proposals(session)
    assert proposal.kind == "inventory_transfer" and proposal.agent_key == "inventario"
    assert proposal.proposed_by_principal_id == airy_id
    assert proposal.subject_type == "product_location"
    assert proposal.subject_id == f"{product_id}:{lince}"
    assert proposal.location_id == lince and proposal.conversation_id is None
    assert proposal.payload == {
        "product_id": product_id,
        "origin_location_id": jm,
        "destination_location_id": lince,
        "quantity": "16.00",
    }
    from app.proposals.executors import normalize_payload, payload_hash

    _args, normalized = normalize_payload("inventory_transfer", proposal.payload)
    assert proposal.payload == normalized
    assert proposal.payload_hash == payload_hash("inventory_transfer", ORG, normalized)
    evidence = proposal.evidence
    assert evidence["run_id"] == body["id"]
    assert evidence["product_id"] == product_id
    assert evidence["product_name"] == LOW_STOCK_PRODUCT and evidence["unit"] == "cartucho"
    assert evidence["subject_version"] == proposal.subject_version
    assert evidence["target_fill"] == "16.00" and evidence["quantity"] == "16.00"
    assert evidence["target"] == {
        "location_id": lince, "name": LINCE, "balance": "4.00", "min_quantity": "10.00",
        "consumption_7d": "0.00",
    }
    assert evidence["donor"] == {
        "location_id": jm, "name": JESUS_MARIA, "balance": "120.00", "min_quantity": "10.00",
        "surplus": "110.00", "consumption_7d": "0.00",
    }
    assert proposal.reason == (
        f"{LOW_STOCK_PRODUCT}: {LINCE} 4.00 < mín. 10.00; traspasar 16.00 desde "
        f"{JESUS_MARIA} (120.00, mín. 10.00)"
    )
    # Dispatch: an inventario run never falls through to the collections sweep.
    assert _count(session, AgentProposal, AgentProposal.kind == "collection_reminder") == 0

    second = _run(client, carlos)
    assert second.json()["counts"] == _counts(candidates=1, deduped=1)
    assert len(_proposals(session)) == 1

    before = len(_movements(session))
    item = _item(client, carlos, proposal.id)
    assert item["kind"] == "inventory_transfer" and item["actions"] == ["approve", "decline"]
    key = str(uuid4())
    approved = _approve(client, carlos, item, key=key)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "executed"
    movements = _movements(session)[before:]
    assert sorted((m.type, m.location_id) for m in movements) == [
        (TRANSFER_IN, lince), (TRANSFER_OUT, jm),
    ]
    assert len({m.transfer_id for m in movements}) == 1
    transfer_id = movements[0].transfer_id
    assert approved.json()["result_ref"] == {"type": "inventory_transfer", "id": transfer_id}
    assert all(m.reason == f"Propuesta #{proposal.id}" for m in movements)
    assert _balance(session, product_id, lince) == Decimal("20.00")
    assert _balance(session, product_id, jm) == Decimal("104.00")

    replay = _approve(client, carlos, item, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["result_ref"] == {"type": "inventory_transfer", "id": transfer_id}
    assert len(_movements(session)) == before + 2


# --- 2. dedupe -----------------------------------------------------------------------


def test_rerun_after_decline_and_same_key_replay_create_nothing_new(client, session):
    _simple(session)
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)

    assert _run(client, agent).json()["counts"] == _counts(candidates=1, proposed=1)
    [proposal] = _proposals(session)
    assert _decline(client, carlos, proposal.id).status_code == 200
    assert _run(client, agent).json()["counts"] == _counts(candidates=1, deduped=1)
    assert len(_proposals(session)) == 1

    key = str(uuid4())
    original = _run(client, agent, key=key)
    replay = _run(client, agent, key=key)
    assert original.status_code == replay.status_code == 201
    assert replay.json()["id"] == original.json()["id"]
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert _count(session, _agent_runs()) == 3


def test_earlier_day_rows_open_dedupes_closed_and_expired_do_not(client, session):
    _simple(session)
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)

    assert _run(client, agent).json()["counts"]["proposed"] == 1
    [declined] = _proposals(session)
    _decline(client, carlos, declined.id)
    session.execute(
        text("UPDATE agent_proposals SET created_at = created_at - interval '1 day' WHERE id = :id"),
        {"id": declined.id},
    )
    session.commit()
    # Declined yesterday: today's run re-plans.
    assert _run(client, agent).json()["counts"] == _counts(candidates=1, proposed=1)
    pending = _proposals(session)[-1]

    session.execute(
        text("UPDATE agent_proposals SET created_at = created_at - interval '1 day' WHERE id = :id"),
        {"id": pending.id},
    )
    session.commit()
    # Pending from yesterday and not expired: still open → deduped.
    assert _run(client, agent).json()["counts"] == _counts(candidates=1, deduped=1)

    session.execute(
        text(
            "UPDATE agent_proposals SET created_at = now() - interval '4 days', "
            "expires_at = now() - interval '1 day' WHERE id = :id"
        ),
        {"id": pending.id},
    )
    session.commit()
    # Pending but past expires_at (never persisted as expired): never holds the subject.
    assert _run(client, agent).json()["counts"] == _counts(candidates=1, proposed=1)
    rows = _proposals(session)
    assert len(rows) == 3
    assert rows[1].status == "expired" and rows[2].status == "pending"


# --- 3. donor choice and allocation --------------------------------------------------


def test_donor_choice_cap_and_entry_fallback(client, session):
    a = _location(session, "Sede A")
    b = _location(session, "Sede B")
    c = _location(session, "Sede C")
    d = _location(session, "Sede D")
    x = _product(session, "Producto X")
    y = _product(session, "Producto Y")
    z = _product(session, "Producto Z")
    # X: B has the largest surplus (110 vs 20) → B gives min(16, 110) = 16.
    _point(session, x, a, balance="4", minimum="10")
    _point(session, x, b, balance="120", minimum="10")
    _point(session, x, c, balance="30", minimum="10")
    # Y: C surplus 5 caps the transfer; B has stock but no reorder point (never a
    # donor); D is itself below its own min (never a donor) → entry 2·5 − 3 = 7.
    _point(session, y, a, balance="2", minimum="10")
    _point(session, y, b, balance="100")
    _point(session, y, c, balance="15", minimum="10")
    _point(session, y, d, balance="3", minimum="5")
    # Z: nobody has surplus (B exactly at its min) → entry 2·4 − 0 = 8.
    _point(session, z, a, balance="0", minimum="4")
    _point(session, z, b, balance="6", minimum="6")
    _airy_id, agent = _airy_caller(session)

    body = _run(client, agent).json()
    assert body["counts"] == _counts(candidates=4, proposed=4)
    got = {(p.kind, p.subject_id): p for p in _proposals(session)}
    assert set(got) == {
        ("inventory_transfer", f"{x}:{a}"),
        ("inventory_transfer", f"{y}:{a}"),
        ("inventory_entry", f"{y}:{d}"),
        ("inventory_entry", f"{z}:{a}"),
    }
    assert got[("inventory_transfer", f"{x}:{a}")].payload == {
        "product_id": x, "origin_location_id": b, "destination_location_id": a,
        "quantity": "16.00",
    }
    capped = got[("inventory_transfer", f"{y}:{a}")]
    assert capped.payload["origin_location_id"] == c and capped.payload["quantity"] == "5.00"
    assert capped.evidence["target_fill"] == "18.00" and capped.evidence["donor"]["surplus"] == "5.00"
    entry = got[("inventory_entry", f"{y}:{d}")]
    assert entry.payload == {"product_id": y, "location_id": d, "quantity": "7.00"}
    assert entry.evidence["donor"] is None and entry.location_id == d
    assert entry.reason.endswith("sin sede donante, reponer 7.00")
    assert got[("inventory_entry", f"{z}:{a}")].payload["quantity"] == "8.00"
    for proposal in got.values():
        assert proposal.evidence["subject_version"] == proposal.subject_version


def test_one_donor_is_split_across_two_targets_and_siblings_supersede(client, session):
    a = _location(session, "Sede A")
    b = _location(session, "Sede B")
    c = _location(session, "Sede C")
    p = _product(session, "Anestesia")
    _point(session, p, a, balance="4", minimum="10")   # fill 16
    _point(session, p, b, balance="2", minimum="10")   # fill 18
    _point(session, p, c, balance="30", minimum="10")  # surplus 20
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)

    assert _run(client, agent).json()["counts"] == _counts(candidates=2, proposed=2)
    first, second = _proposals(session)
    assert first.payload["destination_location_id"] == a
    assert first.payload["quantity"] == "16.00"
    assert second.payload["destination_location_id"] == b
    assert second.payload["quantity"] == "4.00"
    assert Decimal(first.payload["quantity"]) + Decimal(second.payload["quantity"]) <= 20

    assert _approve(client, carlos, _item(client, carlos, first.id)).json()["status"] == "executed"
    # The donor's ledger moved: the sibling's numbers are stale by design.
    sibling = _approve(client, carlos, _item(client, carlos, second.id))
    assert sibling.status_code == 409, sibling.text
    assert sibling.json()["error"]["code"] == "PROPOSAL_SUPERSEDED"
    # Same-day dedupe (any status) holds B until tomorrow's run re-plans.
    assert _run(client, agent).json()["counts"] == _counts(candidates=1, deduped=1)


# --- 5. who may approve ----------------------------------------------------------------


def test_secretaria_may_decline_but_never_approve_or_trigger(client, session):
    seeded = _simple(session)
    _airy_id, agent = _airy_caller(session)
    _lucia_id, lucia = _lucia(session)
    _run(client, agent)
    [proposal] = _proposals(session)

    item = _item(client, lucia, proposal.id)
    assert item["actions"] == ["decline"]
    listed = client.get("/agent/inbox", headers=lucia, params={"kind": "inventory_transfer"})
    assert listed.status_code == 200, listed.text
    assert [i["id"] for i in listed.json()["items"]] == [proposal.id]
    denied = _approve(client, lucia, item)
    assert denied.status_code == 403, denied.text
    assert _proposals(session)[0].status == "pending"
    declined = _decline(client, lucia, proposal.id)
    assert declined.status_code == 200, declined.text
    assert declined.json()["status"] == "declined"

    trigger = _run(client, lucia)
    assert trigger.status_code == 403, trigger.text
    assert _count(session, _agent_runs()) == 1
    assert _balance(session, seeded["p"], seeded["a"]) == Decimal("4.00")


# --- 6. drift ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drift",
    [
        lambda s, ids: _stock(s, ids["p"], ids["a"], "1"),  # entry at destination
        lambda s, ids: _adjust(s, ids["p"], ids["b"], "-1"),  # stock-out at origin
        lambda s, ids: _adjust(s, ids["p"], ids["b"], "-110"),  # origin drained below 16
    ],
    ids=["destination-entry", "origin-out", "origin-drained"],
)
def test_ledger_drift_before_approval_supersedes(client, session, drift):
    ids = _simple(session)
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)
    _run(client, agent)
    [proposal] = _proposals(session)
    item = _item(client, carlos, proposal.id)
    drift(session, ids)
    before = len(_movements(session))

    response = _approve(client, carlos, item)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_SUPERSEDED"
    assert _proposals(session)[0].status == "superseded"
    assert len(_movements(session)) == before


# --- 7. entry ----------------------------------------------------------------------------


def test_entry_approval_registers_one_entrada_without_price(client, session):
    a = _location(session, "Sede A")
    p = _product(session, "Guantes")
    _point(session, p, a, balance="1", minimum="5")
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)
    _run(client, agent)
    [proposal] = _proposals(session)
    assert proposal.kind == "inventory_entry"
    assert proposal.payload == {"product_id": p, "location_id": a, "quantity": "9.00"}
    before = len(_movements(session))

    approved = _approve(client, carlos, _item(client, carlos, proposal.id))
    assert approved.status_code == 200, approved.text
    [movement] = _movements(session)[before:]
    assert (movement.type, movement.location_id, movement.quantity) == (ENTRADA, a, Decimal("9.00"))
    assert movement.unit_price is None
    assert approved.json()["result_ref"] == {"type": "inventory_movement", "id": movement.id}
    assert _balance(session, p, a) == Decimal("10.00")


def test_executors_replay_returns_the_same_result_ref(session):
    from test_domain_gaps_helpers import actor_ctx

    from app.iam.permissions import MOVEMENTS_CREATE
    from app.proposals.executors import KINDS, Execution, load_args

    a = _location(session, "Sede A")
    b = _location(session, "Sede B")
    p = _product(session, "Anestesia")
    _stock(session, p, b, "50")
    ctx = actor_ctx(session, codes=(MOVEMENTS_CREATE,))
    session.rollback()
    cases = {
        "inventory_transfer": {"product_id": p, "origin_location_id": b,
                               "destination_location_id": a, "quantity": "5.00"},
        "inventory_entry": {"product_id": p, "location_id": a, "quantity": "3.00"},
    }
    for kind, payload in cases.items():
        item = Execution(proposal_id=1, execution_key=uuid.uuid4(), conversation_id=None,
                         args=load_args(kind, payload))
        first = KINDS[kind].execute(session, ctx, item)
        count = len(_movements(session))
        again = KINDS[kind].execute(session, ctx, item)
        assert again == first, kind
        assert len(_movements(session)) == count, kind
    assert _balance(session, p, a) == Decimal("8.00")


# --- 8. gate, kill switch, provisioning, validation ----------------------------------------


def test_kill_switch_refuses_and_leaves_no_trace(client, session, monkeypatch):
    _simple(session)
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)
    monkeypatch.setenv("AGENT_INVENTARIO_ENABLED", "false")
    for headers in (agent, carlos):
        response = _run(client, headers)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["details"] == {"agent_key": "inventario",
                                                       "reason": "disabled"}
    assert _count(session, _agent_runs()) == 0
    assert _count(session, AgentProposal) == 0


def _missing(session):
    return None


def _other_org_only(session):
    _airy(session, organization_id=_other_org(session))


def _without_permission(session):
    principal = create_principal(session, display_name=AIRY, principal_type="agent")
    add_membership(session, organization_id=ORG, principal_id=principal.id)
    session.commit()


@pytest.mark.parametrize("provision", [_missing, _other_org_only, _without_permission])
def test_human_trigger_needs_a_provisioned_inventory_proposer(client, session, provision):
    _simple(session)
    provision(session)
    _carlos_id, carlos = _carlos(session)
    response = _run(client, carlos)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"] == {"agent_key": "inventario",
                                                   "reason": "not_provisioned"}
    assert _count(session, _agent_runs()) == 0


def test_agent_without_the_inventory_reads_is_denied(client, session):
    _simple(session)
    _cid, collections = _credential(session, name="airy-cobranza", principal_type="agent",
                                    profile="collections-agent")
    response = _run(client, collections)
    assert response.status_code == 403, response.text
    assert _count(session, _agent_runs()) == 0
    assert _run(client, collections, body={"agent_key": "inventarios"}).status_code == 422


def test_inventario_runs_are_listed_with_their_agent_key(client, session):
    _simple(session)
    _airy_id, agent = _airy_caller(session)
    run = _run(client, agent).json()
    listed = client.get("/agent-runs?agent_key=inventario", headers=agent)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [run["id"]]


# --- 9. tenant isolation --------------------------------------------------------------------


def test_runs_and_approvals_are_tenant_scoped(client, session):
    _simple(session)
    other = _other_org(session)
    _a, agent_a = _airy_caller(session)
    _b, agent_b = _airy_caller(session, organization_id=other)

    assert _run(client, agent_a).json()["counts"]["proposed"] == 1
    assert _run(client, agent_b).json()["counts"] == _counts()
    [proposal] = _proposals(session)
    assert proposal.organization_id == ORG

    foreign_admin = _credential(session, name="Admin B", principal_type="human",
                                profile="administrador", organization_id=other)[1]
    item = {"id": proposal.id, "payload_hash": proposal.payload_hash}
    assert _approve(client, foreign_admin, item).status_code == 404
    assert _proposals(session)[0].status == "pending"
