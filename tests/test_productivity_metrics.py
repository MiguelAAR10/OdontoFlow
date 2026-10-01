"""B3 — administrator productivity, computed by SQL on the fly.

Spec: ``docs/superpowers/specs/2026-10-01-erp-b3.md``. Every expected value is
recomputed in Python from raw rows (independent of the SQL under test).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import and_, select, text
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import (
    _agent,
    _approve,
    _backdate,
    _carlos,
    _create,
    _credential,
    _decline,
    _lucia,
    _other_org,
    _reminder,
    _seed_charge,
)
from test_domain_gaps_helpers import actor_ctx, pay

from app import create_app
from app.audit.models import AuditEvent
from app.audit.service import record_event
from app.clinical.models import ServiceExecution, Visit
from app.db import get_db
from app.economics.models import Charge, Payment, PaymentReversal
from app.economics.schemas import PaymentReverse
from app.economics.service import reverse_payment
from app.iam.permissions import PAYMENTS_REVERSE
from app.organization.models import Location
from app.proposals.models import AgentProposal
from app.scheduling.models import Appointment, AppointmentProposal
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = ZoneInfo("America/Lima")


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


# --- independent expectation ------------------------------------------------------


def _local(instant: datetime, tz_name: str | None) -> date:
    return instant.astimezone(ZoneInfo(tz_name or "America/Lima")).date()


def _expected(session, start: date, end: date, location_id: int | None = None) -> dict:
    session.expire_all()
    now = datetime.now(UTC)
    tz = {loc.id: loc.timezone for loc in session.scalars(select(Location))}

    def in_range(instant, location):
        return start <= _local(instant, tz.get(location)) <= end

    def at(location):
        return location_id is None or location == location_id

    appointments = {"completed": 0, "no_show": 0, "cancelled": 0}
    for appt in session.scalars(select(Appointment).where(Appointment.organization_id == ORG)):
        if appt.state in appointments and at(appt.location_id) and in_range(
            appt.start_utc, appt.location_id
        ):
            appointments[appt.state] += 1

    reversed_ids = set(session.scalars(select(PaymentReversal.payment_id)))
    charges = session.execute(
        select(Charge, Visit.location_id)
        .join(ServiceExecution, and_(ServiceExecution.organization_id == Charge.organization_id,
                                     ServiceExecution.id == Charge.service_execution_id))
        .join(Visit, and_(Visit.organization_id == ServiceExecution.organization_id,
                          Visit.id == ServiceExecution.visit_id))
        .where(Charge.organization_id == ORG)
    ).all()
    payments = session.scalars(select(Payment).where(Payment.organization_id == ORG)).all()
    location_of = {charge.id: loc for charge, loc in charges}
    charged = collected = outstanding = Decimal("0")
    for charge, loc in charges:
        if at(loc) and in_range(charge.created_at, loc):
            charged += charge.amount
            paid = sum(
                (p.amount for p in payments if p.charge_id == charge.id and p.id not in reversed_ids),
                Decimal("0"),
            )
            outstanding += charge.amount - paid
    for p in payments:
        loc = location_of[p.charge_id]
        if p.id not in reversed_ids and at(loc) and in_range(p.paid_at, loc):
            collected += p.amount

    proposals = defaultdict(lambda: {"created": 0, "approved": 0, "declined": 0, "expired": 0})
    reminders = 0
    for prop in session.scalars(select(AgentProposal).where(AgentProposal.organization_id == ORG)):
        if not (at(prop.location_id) and in_range(prop.created_at, prop.location_id)):
            continue
        bucket = proposals[prop.agent_key]
        bucket["created"] += 1
        approved = prop.decided_by_principal_id is not None and prop.status != "declined"
        bucket["approved"] += approved
        bucket["declined"] += prop.status == "declined"
        bucket["expired"] += prop.status == "expired" or (
            prop.status == "pending" and prop.expires_at <= now
        )
        reminders += approved and prop.kind == "collection_reminder"
    declined_ids = {
        int(e) for e in session.scalars(
            select(AuditEvent.entity_id).where(AuditEvent.action == "appointment_proposal.declined")
        )
    }
    for prop in session.scalars(
        select(AppointmentProposal).where(AppointmentProposal.organization_id == ORG)
    ):
        if not (at(prop.location_id) and in_range(prop.created_at, prop.location_id)):
            continue
        bucket = proposals["reception"]
        bucket["created"] += 1
        bucket["approved"] += prop.status == "confirmed"
        declined = prop.id in declined_ids
        bucket["declined"] += declined
        bucket["expired"] += (prop.status == "expired" and not declined) or (
            prop.status == "pending" and prop.expires_at <= now
        )
    for key in ("cobranza", "reception"):
        proposals[key]  # always listed
    session.rollback()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "location_id": location_id,
        "appointments": appointments,
        "money": {
            "currency": "PEN",
            "charged": f"{charged:.2f}",
            "collected": f"{collected:.2f}",
            "outstanding": f"{outstanding:.2f}",
        },
        "proposals": [{"agent_key": k, **v} for k, v in sorted(proposals.items())],
        "collection_reminders_approved": reminders,
    }


# --- fixture data -------------------------------------------------------------------


def _seed_world(client, session) -> dict:
    from scripts.seed_demo import seed_demo

    anchor = datetime.now(LIMA).date()
    seed_demo(session, organization_id=ORG, anchor=anchor)
    # Outcomes on past seeded appointments: one of each terminal state.
    past = session.scalars(
        select(Appointment.id)
        .where(Appointment.organization_id == ORG, Appointment.start_utc < datetime.now(UTC))
        .order_by(Appointment.id)
        .limit(5)
    ).all()
    for appointment_id, state in zip(past, ("completed", "completed", "no_show", "cancelled")):
        session.execute(
            text("UPDATE appointments SET state = :s WHERE id = :id"),
            {"s": state, "id": appointment_id},
        )
    session.commit()

    # Money: a reversed payment on a fresh charge.
    seeded = _seed_charge(session)
    payment_id = pay(session, seeded["charge_id"], "50.00")
    pay(session, seeded["charge_id"], "20.00")
    reverse_payment(
        session, payment_id, PaymentReverse(reason="Duplicado"),
        ctx=actor_ctx(session, codes=(PAYMENTS_REVERSE,)),
    )

    # Agent proposals: approved (executed), declined, lazily expired, still pending.
    _agent_id, agent = _agent(session)
    _lucia_id, lucia = _lucia(session)
    created = []
    for _ in range(4):
        charge = _seed_charge(session)
        response = _create(client, agent, "collection_reminder", _reminder(charge["charge_id"]))
        assert response.status_code == 201, response.text
        created.append(response.json())
    assert _approve(client, lucia, created[0]).status_code == 200
    assert _decline(client, lucia, created[1]["id"]).status_code == 200
    _backdate(session, created[2]["id"])  # pending, expires_at in the past: no sweep

    # Appointment proposals (reception): confirmed, declined, lazily expired, live.
    from test_appointment_proposals import _seed_proposal, _seed_tenant

    tenant = _seed_tenant(session, organization_id=ORG, suffix="b3-metrics", phone="+51911222333")
    start = datetime(2027, 1, 4, 15, tzinfo=UTC)
    _seed_proposal(session, tenant, start_utc=start, status="confirmed")
    declined = _seed_proposal(session, tenant, start_utc=start + timedelta(hours=1), status="expired")
    _decline_audit(session, declined.id)
    stale = _seed_proposal(session, tenant, start_utc=start + timedelta(hours=2))
    session.execute(
        text(
            "UPDATE appointment_proposals SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 hour' WHERE id = :id"
        ),
        {"id": stale.id},
    )
    session.commit()
    _seed_proposal(session, tenant, start_utc=start + timedelta(hours=3))
    return {"anchor": anchor, "location_id": seeded["location_id"],
            "tenant_location": tenant["location"].id}


def _decline_audit(session, proposal_id: int) -> None:
    ctx = actor_ctx(session, codes=())
    with session.begin():
        record_event(
            session,
            ctx=ctx,
            entity_type="appointment_proposal",
            entity_id=str(proposal_id),
            action="appointment_proposal.declined",
            before_state={"status": "pending"},
            after_state={"status": "expired"},
        )


def _report(client, headers, **params):
    return client.get("/metrics/productivity", params=params, headers=headers)


# --- 9. values match the raw rows ---------------------------------------------------


def test_productivity_matches_independent_computation(client, session):
    world = _seed_world(client, session)
    _cid, carlos = _carlos(session)
    start, end = world["anchor"] - timedelta(days=60), world["anchor"]

    response = _report(client, carlos, **{"from": start.isoformat(), "to": end.isoformat()})
    assert response.status_code == 200, response.text
    body = response.json()
    expected = _expected(session, start, end)
    assert body == expected
    # The fixture actually exercises every bucket.
    assert body["appointments"]["completed"] >= 2 and body["appointments"]["no_show"] >= 1
    assert body["collection_reminders_approved"] == 1
    reception = next(p for p in body["proposals"] if p["agent_key"] == "reception")
    assert reception["declined"] >= 1 and reception["expired"] >= 1 and reception["approved"] >= 1
    by_key = {p["agent_key"]: p for p in body["proposals"]}
    assert sum(p["expired"] for p in by_key.values()) >= 2
    assert sum(p["declined"] for p in by_key.values()) >= 2

    for location_id in (world["location_id"], world["tenant_location"]):
        scoped = _report(
            client, carlos,
            **{"from": start.isoformat(), "to": end.isoformat(), "location_id": location_id},
        )
        assert scoped.status_code == 200, scoped.text
        assert scoped.json() == _expected(session, start, end, location_id)
    # The reversed S/ 50 never counts; the S/ 20 does (fresh charge, own location).
    fresh = _expected(session, start, end, world["location_id"])["money"]
    assert fresh["collected"] == "20.00" and fresh["outstanding"] == "130.00"


# --- 10. permissions, isolation, validation -------------------------------------------


def test_only_administrador_reads_productivity_and_input_is_validated(client, session):
    _lid, lucia = _lucia(session)
    _cid, carlos = _carlos(session)
    _aid, agent = _credential(
        session, name="b3-metrics-agent", principal_type="agent", profile="collections-agent"
    )
    params = {"from": "2026-09-01", "to": "2026-09-30"}
    for headers in (lucia, agent):
        denied = _report(client, headers, **params)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert _report(client, carlos, **params).status_code == 200

    # Org isolation: org B's charge never reaches org A's report.
    other = _other_org(session, "Clinica B3 metrics")
    _seed_charge(session, organization_id=other, conversation=False)
    today = datetime.now(LIMA).date()
    report = _report(client, carlos, **{"from": (today - timedelta(days=1)).isoformat(),
                                         "to": today.isoformat()}).json()
    assert report["money"]["charged"] == "0.00"

    for bad in (
        {"from": "2026-09-30", "to": "2026-09-01"},
        {"from": "2026-01-01", "to": "2026-09-30"},
        {"from": "2026-09-01T00:00:00", "to": "2026-09-30"},
        {"from": "not-a-date", "to": "2026-09-30"},
        {"to": "2026-09-30"},
        {**params, "extra": "1"},
    ):
        response = _report(client, carlos, **bad)
        assert response.status_code == 422, (bad, response.text)
        assert response.json()["error"]["code"] == "INVALID_INPUT"
