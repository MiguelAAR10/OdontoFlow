"""COB — collections agent: deterministic sweep, 'run now', reminder proposals.

Spec: ``docs/superpowers/specs/2026-10-01-erp-cob.md``. Real PostgreSQL, one
pytest process. The sweep only *proposes*; B2's approve executes as a human.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import and_, select, text
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import (
    _approve,
    _carlos,
    _count,
    _credential,
    _decline,
    _human_with,
    _lucia,
    _other_org,
    _seed_charge,
)
from test_domain_gaps_helpers import actor_ctx, pay

from app import create_app
from app.audit.models import AuditEvent
from app.clinical.models import ServiceExecution, Visit
from app.db import get_db
from app.economics.models import Charge
from app.economics.schemas import PaymentReverse
from app.economics.service import charge_paid_amount, reverse_payment
from app.errors import AppError
from app.events.models import DomainEvent
from app.iam.permissions import CHARGES_READ, PAYMENTS_REVERSE, PROPOSALS_READ
from app.iam.service import (
    PERMISSION_DENIED_HTTP_STATUS,
    PERMISSION_DENIED_MESSAGE,
    IamErrorCode,
    add_membership,
    create_principal,
)
from app.idempotency.models import CommandReceipt
from app.messaging.models import OutboundMessage
from app.organization.models import Location
from app.proposals.executors import normalize_payload, payload_hash, reachable_conversation
from app.proposals.models import AgentProposal
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import _assign_profile, _resolve_principal

LIMA = ZoneInfo("America/Lima")
AIRY = "airy-cobranza"


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
    # A developer shell must not leak the kill switch into the suite.
    monkeypatch.delenv("AGENT_COBRANZA_ENABLED", raising=False)


# --- helpers ------------------------------------------------------------------


def _agent_runs():
    from app.agents_runtime.models import AgentRun

    return AgentRun


def _airy(session, organization_id=ORG) -> int:
    """Provision the org's collections proposer (no credential)."""
    principal = _resolve_principal(
        session, organization_id=organization_id, name=AIRY, principal_type="agent"
    )
    _assign_profile(
        session, organization_id=organization_id, principal_id=principal.id,
        profile="collections-agent",
    )
    session.commit()
    return principal.id


def _airy_caller(session, organization_id=ORG):
    return _credential(
        session, name=AIRY, principal_type="agent", profile="collections-agent",
        organization_id=organization_id,
    )


def _age(session, charge_id: int, days: int) -> None:
    session.execute(
        text("UPDATE charges SET created_at = now() - make_interval(days => :d) WHERE id = :id"),
        {"d": days, "id": charge_id},
    )
    session.commit()


def _overdue(session, *, days=10, **kwargs) -> dict:
    seeded = _seed_charge(session, **kwargs)
    _age(session, seeded["charge_id"], days)
    return seeded


def _run(client, headers, *, key=None, body=None, send_key=True):
    request_headers = dict(headers)
    if send_key:
        request_headers["Idempotency-Key"] = key or str(uuid4())
    return client.post(
        "/agent-runs", json=body if body is not None else {"agent_key": "cobranza"},
        headers=request_headers,
    )


def _proposals(session, *where):
    session.expire_all()
    rows = session.scalars(select(AgentProposal).where(*where).order_by(AgentProposal.id)).all()
    session.rollback()
    return rows


def _run_row(session, run_id):
    session.expire_all()
    row = session.get(_agent_runs(), run_id)
    session.rollback()
    return row


def _audit_actions(session, entity_type, entity_id=None):
    where = [AuditEvent.entity_type == entity_type]
    if entity_id is not None:
        where.append(AuditEvent.entity_id == str(entity_id))
    session.expire_all()
    rows = session.scalars(select(AuditEvent).where(*where).order_by(AuditEvent.id)).all()
    session.rollback()
    return rows


def _expected_candidates(session, organization_id=ORG) -> list[tuple[int, bool]]:
    """Independent count: net-of-reversals balance, location-tz age, reachability."""
    rows = session.execute(
        select(Charge.id, Charge.amount, Charge.created_at, Location.timezone, Visit.patient_id)
        .join(
            ServiceExecution,
            and_(
                ServiceExecution.organization_id == Charge.organization_id,
                ServiceExecution.id == Charge.service_execution_id,
            ),
        )
        .join(Visit, and_(Visit.organization_id == ServiceExecution.organization_id,
                          Visit.id == ServiceExecution.visit_id))
        .join(Location, and_(Location.organization_id == Visit.organization_id,
                             Location.id == Visit.location_id))
        .where(Charge.organization_id == organization_id)
    ).all()
    expected = []
    for charge_id, amount, created_at, tz_name, patient_id in rows:
        zone = ZoneInfo(tz_name)
        age = (datetime.now(zone).date() - created_at.astimezone(zone).date()).days
        balance = amount - charge_paid_amount(session, charge_id, organization_id)
        if balance > 0 and age >= 7:
            reachable = reachable_conversation(session, organization_id, patient_id) is not None
            expected.append((charge_id, reachable))
    session.rollback()
    return sorted(expected)


# --- 1/2/4. demo seed end to end -----------------------------------------------


def test_demo_seed_run_proposes_the_s180_reminder_and_lucia_approves_it(client, session):
    from scripts.seed_demo import seed_demo

    seed_demo(session, organization_id=ORG, anchor=datetime.now(LIMA).date())
    seed_demo(session, organization_id=ORG, anchor=datetime.now(LIMA).date())  # idempotent
    _agent_id, agent = _airy_caller(session)
    expected = _expected_candidates(session)
    reachable = [charge_id for charge_id, ok in expected if ok]
    assert len(reachable) == 1  # only the S/ 180 patient has a conversation

    first = _run(client, agent)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "completed" and body["trigger"] == "manual"
    assert body["counts"] == {
        "candidates": len(expected),
        "proposed": 1,
        "deduped": 0,
        "skipped": len(expected) - 1,
    }
    [proposal] = _proposals(session)
    assert proposal.kind == "collection_reminder" and proposal.agent_key == "cobranza"
    assert int(proposal.subject_id) == reachable[0]
    assert "S/ 180.00" in proposal.payload["message_text"]
    assert "S/ 180.00" in proposal.reason and "12 días" in proposal.reason
    assert proposal.evidence["days_since_issued"] == 12
    assert proposal.evidence["run_id"] == body["id"]
    assert proposal.evidence["balance"] == "180.00"
    _args, normalized = normalize_payload("collection_reminder", proposal.payload)
    assert proposal.payload == normalized
    assert proposal.payload_hash == payload_hash("collection_reminder", ORG, normalized)

    second = _run(client, agent)
    assert second.status_code == 201, second.text
    assert second.json()["counts"] == {
        "candidates": len(expected),
        "proposed": 0,
        "deduped": 1,
        "skipped": len(expected) - 1,
    }
    assert len(_proposals(session)) == 1
    assert _count(session, _agent_runs()) == 2

    lucia_id, lucia = _lucia(session)
    item = client.get(f"/agent/proposals/{proposal.id}", headers=lucia).json()
    approved = _approve(client, lucia, item)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "executed"
    session.expire_all()
    outbound = session.scalars(select(OutboundMessage)).all()
    session.rollback()
    assert len(outbound) == 1
    assert outbound[0].conversation_id == proposal.conversation_id
    assert outbound[0].payload["text"] == proposal.payload["message_text"]
    executed = [a for a in _audit_actions(session, "agent_proposal", proposal.id)
                if a.action == "agent_proposal.executed"]
    assert [(a.actor_id, a.actor_type) for a in executed] == [(str(lucia_id), "human")]


# --- 2. rerun, decline, replay, earlier-day dedupe -------------------------------


def test_rerun_after_decline_and_same_key_replay_create_nothing_new(client, session):
    reachable = _overdue(session)
    _overdue(session, conversation=False)
    _airy_id, agent = _airy_caller(session)
    _lucia_id, lucia = _lucia(session)

    first = _run(client, agent).json()
    assert first["counts"] == {"candidates": 2, "proposed": 1, "deduped": 0, "skipped": 1}
    [proposal] = _proposals(session)
    assert int(proposal.subject_id) == reachable["charge_id"]

    assert _decline(client, lucia, proposal.id).status_code == 200
    again = _run(client, agent).json()
    assert again["counts"] == {"candidates": 2, "proposed": 0, "deduped": 1, "skipped": 1}
    assert len(_proposals(session)) == 1

    key = str(uuid4())
    original = _run(client, agent, key=key)
    replay = _run(client, agent, key=key)
    assert original.status_code == replay.status_code == 201
    assert replay.json()["id"] == original.json()["id"]
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert _count(session, _agent_runs()) == 3


def test_a_pending_proposal_from_an_earlier_day_is_deduped(client, session):
    _overdue(session)
    _airy_id, agent = _airy_caller(session)
    assert _run(client, agent).json()["counts"]["proposed"] == 1
    session.execute(text("UPDATE agent_proposals SET created_at = created_at - interval '1 day'"))
    session.commit()

    body = _run(client, agent).json()
    assert body["counts"] == {"candidates": 1, "proposed": 0, "deduped": 1, "skipped": 0}
    assert len(_proposals(session)) == 1


def test_a_failed_run_replays_as_failed_without_a_second_sweep(client, session, monkeypatch):
    import app.agents_runtime.cobranza as cobranza

    _overdue(session)
    _airy_id, agent = _airy_caller(session)

    def boom(**_kwargs):
        raise RuntimeError("draft exploded")

    monkeypatch.setattr(cobranza, "draft_reminder", boom)
    key = str(uuid4())
    failed = _run(client, agent, key=key)
    assert failed.status_code == 500
    [run] = session.scalars(select(_agent_runs())).all()
    session.rollback()
    assert run.status == "failed" and run.error_category == "unexpected"
    assert run.finished_at is not None
    assert "agent_run.failed" in {a.action for a in _audit_actions(session, "agent_run", run.id)}

    monkeypatch.undo()
    replay = _run(client, agent, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == run.id and replay.json()["status"] == "failed"
    assert _count(session, _agent_runs()) == 1
    assert _proposals(session) == []


# --- 3. candidate rules --------------------------------------------------------


def test_candidate_rules_balance_age_reversal_reachability(client, session):
    young = _overdue(session, days=6)
    boundary = _overdue(session, days=7)
    paid = _overdue(session)
    pay(session, paid["charge_id"], "150.00")
    reversed_ = _overdue(session)
    payment_id = pay(session, reversed_["charge_id"], "150.00")
    reverse_payment(
        session, payment_id, PaymentReverse(reason="Duplicado"),
        ctx=actor_ctx(session, codes=(PAYMENTS_REVERSE,)),
    )
    _overdue(session, conversation=False)
    _overdue(session, consent="opted_out")
    _airy_id, agent = _airy_caller(session)

    body = _run(client, agent).json()
    assert body["counts"] == {"candidates": 4, "proposed": 2, "deduped": 0, "skipped": 2}
    subjects = {int(p.subject_id) for p in _proposals(session)}
    assert subjects == {boundary["charge_id"], reversed_["charge_id"]}
    assert young["charge_id"] not in subjects


# --- 5. human trigger and the derived proposer -----------------------------------


def test_admin_and_secretaria_trigger_runs_proposed_by_airy(client, session):
    _overdue(session)
    airy_id = _airy(session)
    carlos_id, carlos = _carlos(session)
    lucia_id, lucia = _lucia(session)

    by_carlos = _run(client, carlos, send_key=False)
    assert by_carlos.status_code == 201, by_carlos.text
    assert by_carlos.json()["triggered_by_principal_id"] == carlos_id
    [proposal] = _proposals(session)
    assert proposal.proposed_by_principal_id == airy_id
    assert proposal.agent_key == "cobranza"

    by_lucia = _run(client, lucia)
    assert by_lucia.status_code == 201, by_lucia.text
    assert by_lucia.json()["triggered_by_principal_id"] == lucia_id
    assert by_lucia.json()["counts"]["deduped"] == 1

    # The human path only proposes: nothing executed, nothing queued.
    assert _proposals(session)[0].status == "pending"
    assert _count(session, OutboundMessage) == 0
    assert _count(session, AuditEvent, AuditEvent.action == "agent_proposal.executed") == 0


def _airy_missing(session):
    return None


def _airy_inactive(session):
    _airy(session)
    session.execute(
        text(
            "UPDATE memberships SET is_active = false WHERE organization_id = :org AND "
            "principal_id = (SELECT id FROM principals WHERE display_name = :name)"
        ),
        {"org": ORG, "name": AIRY},
    )
    session.commit()


def _airy_other_org_only(session):
    _airy(session, organization_id=_other_org(session))


def _airy_without_permission(session):
    principal = create_principal(session, display_name=AIRY, principal_type="agent")
    add_membership(session, organization_id=ORG, principal_id=principal.id)
    session.commit()


@pytest.mark.parametrize(
    "provision",
    [_airy_missing, _airy_inactive, _airy_other_org_only, _airy_without_permission],
)
def test_human_trigger_needs_a_provisioned_proposer(client, session, provision):
    _overdue(session)
    provision(session)
    _carlos_id, carlos = _carlos(session)

    response = _run(client, carlos)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "AGENT_DISABLED"
    assert error["details"] == {"agent_key": "cobranza", "reason": "not_provisioned"}
    assert _count(session, _agent_runs()) == 0
    assert _count(session, AgentProposal) == 0
    assert _count(session, CommandReceipt, CommandReceipt.operation == "agent_run.start") == 0


def test_callers_without_the_gate_permissions_are_denied(client, session):
    _overdue(session)
    _airy(session)
    _hid, human = _human_with(session, (CHARGES_READ, PROPOSALS_READ))
    _aid, weak_agent = _credential(
        session, name="agente-sin-permiso", principal_type="agent", profile="connectivity"
    )
    for headers in (human, weak_agent):
        response = _run(client, headers)
        assert response.status_code == 403, response.text
    assert _count(session, _agent_runs()) == 0
    assert _count(session, AgentProposal) == 0


# --- 6. kill switch ------------------------------------------------------------


def test_kill_switch_refuses_both_paths_and_leaves_no_trace(client, session, monkeypatch):
    _overdue(session)
    _airy_id, agent = _airy_caller(session)
    _carlos_id, carlos = _carlos(session)
    _lucia_id, lucia = _lucia(session)
    monkeypatch.setenv("AGENT_COBRANZA_ENABLED", "false")

    for headers in (agent, carlos):
        response = _run(client, headers)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "AGENT_DISABLED"
        assert response.json()["error"]["details"] == {
            "agent_key": "cobranza", "reason": "disabled",
        }
    assert _count(session, _agent_runs()) == 0
    assert _count(session, AgentProposal) == 0
    assert _count(session, CommandReceipt, CommandReceipt.operation == "agent_run.start") == 0

    listed = client.get("/agent-runs", headers=lucia)
    assert listed.status_code == 200 and listed.json() == {"items": []}


# --- 7. run row, audit, domain event, failures -----------------------------------


def test_run_row_audit_domain_event_and_listing(client, session):
    _overdue(session)
    agent_id, agent = _airy_caller(session)
    _lucia_id, lucia = _lucia(session)

    first = _run(client, agent).json()
    second = _run(client, agent).json()
    row = _run_row(session, first["id"])
    assert row.organization_id == ORG and row.triggered_by_principal_id == agent_id
    assert (row.candidates_count, row.proposed_count, row.deduped_count, row.skipped_count) == (
        1, 1, 0, 0,
    )
    assert row.status == "completed" and row.finished_at >= row.started_at
    assert first["finished_at"] is not None and first["error_category"] is None

    actions = [a.action for a in _audit_actions(session, "agent_run", first["id"])]
    assert actions == ["agent_run.started", "agent_run.completed"]
    session.expire_all()
    events = session.scalars(
        select(DomainEvent).where(
            DomainEvent.event_type == "agent_run.completed",
            DomainEvent.aggregate_id == str(first["id"]),
        )
    ).all()
    session.rollback()
    assert len(events) == 1 and events[0].aggregate_type == "agent_run"
    assert events[0].payload == {"candidates": 1, "proposed": 1, "deduped": 0, "skipped": 0}

    listed = client.get("/agent-runs?agent_key=cobranza&limit=1", headers=lucia)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [second["id"]]
    both = client.get("/agent-runs", headers=lucia).json()["items"]
    assert [r["id"] for r in both] == [second["id"], first["id"]]
    assert client.get("/agent-runs?limit=101", headers=lucia).status_code == 422


def test_a_permission_error_while_proposing_fails_the_run(client, session, monkeypatch):
    import app.agents_runtime.cobranza as cobranza

    _overdue(session)
    _airy_id, agent = _airy_caller(session)

    def denied(*_args, **_kwargs):
        raise AppError(
            IamErrorCode.PERMISSION_DENIED, PERMISSION_DENIED_MESSAGE, details={},
            http_status=PERMISSION_DENIED_HTTP_STATUS,
        )

    monkeypatch.setattr(cobranza, "create_proposal", denied)
    response = _run(client, agent)
    assert response.status_code == 403, response.text
    [run] = session.scalars(select(_agent_runs())).all()
    session.rollback()
    assert run.status == "failed" and run.error_category == "PERMISSION_DENIED"


# --- 8. tenant isolation --------------------------------------------------------


def test_runs_are_tenant_scoped(client, session):
    _overdue(session)
    other = _other_org(session)
    _a, agent_a = _airy_caller(session)
    _b, agent_b = _airy_caller(session, organization_id=other)

    run_a = _run(client, agent_a).json()
    run_b = _run(client, agent_b).json()
    assert run_a["counts"]["proposed"] == 1
    assert run_b["counts"] == {"candidates": 0, "proposed": 0, "deduped": 0, "skipped": 0}
    assert {p.organization_id for p in _proposals(session)} == {ORG}
    assert [r["id"] for r in client.get("/agent-runs", headers=agent_b).json()["items"]] == [
        run_b["id"]
    ]
    assert [r["id"] for r in client.get("/agent-runs", headers=agent_a).json()["items"]] == [
        run_a["id"]
    ]


# --- 9. request validation --------------------------------------------------------


def test_body_and_key_validation(client, session):
    _airy_id, agent = _airy_caller(session)
    for body in ({"agent_key": "inventario"}, {"agent_key": "cobranza", "trigger": "event"}, {}):
        response = _run(client, agent, body=body)
        assert response.status_code == 422, response.text
    assert _run(client, agent, send_key=False).status_code == 422
    assert _count(session, _agent_runs()) == 0

