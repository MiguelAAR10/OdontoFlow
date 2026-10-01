"""B3 — activity feed (audit ∪ proposals ∪ agent runs) and reception turn runs.

Spec: ``docs/superpowers/specs/2026-10-01-erp-b3.md``. Real PostgreSQL, one
pytest process.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import _approve, _credential, _lucia, _other_org
from test_collections_sweep import _airy, _overdue, _run
from test_domain_gaps_helpers import book, seed_booking
from test_staff_reads import _conversation, _message

from app import create_app
from app.agents_runtime.models import AgentRun
from app.audit.models import AuditEvent
from app.audit.service import record_event
from app.context import default_context
from app.db import get_db
from app.iam.context import ExecutionContext
from app.proposals.models import AgentProposal
from app.scheduling.models import AppointmentProposal
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from sales_agent.config import AgentSettings
from sales_agent.schemas import (
    AgentUnavailableError,
    SalesAgentTurnRequest,
    SalesAgentTurnResponse,
)

SALES_AGENT = "airy-recepcion-b3"


@pytest.fixture
def maker(migrated_engine):
    return sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client(maker):
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
def _enabled(monkeypatch):
    monkeypatch.delenv("AGENT_COBRANZA_ENABLED", raising=False)


@pytest.fixture(autouse=True, scope="module")
def _undo_sales_agent_import():
    """Leave no ``sales_agent.api`` module or loggers behind for later tests.

    Importing ``sales_agent.api`` here creates the ``sales_agent.diagnostics``
    logger before ``test_migrations`` runs alembic, whose ``fileConfig`` then
    disables every pre-existing logger — and the sales agent caplog tests that
    run afterwards see no records. The other sales agent test files import the
    module lazily after that point, so undo our import to keep that order.
    """
    modules_before = set(sys.modules)
    loggers_before = set(logging.Logger.manager.loggerDict)
    yield
    for name in set(sys.modules) - modules_before:
        if name == "sales_agent" or name.startswith("sales_agent."):
            parent, _, child = name.rpartition(".")
            if parent in sys.modules and getattr(sys.modules[parent], child, None) is sys.modules[name]:
                delattr(sys.modules[parent], child)
            del sys.modules[name]
    for name in set(logging.Logger.manager.loggerDict) - loggers_before:
        if name == "sales_agent" or name.startswith("sales_agent."):
            del logging.Logger.manager.loggerDict[name]


# --- sales agent harness ----------------------------------------------------------


class FakeRuntime:
    def __init__(self, error: BaseException | None = None):
        self.error = error

    def turn(self, request: SalesAgentTurnRequest) -> SalesAgentTurnResponse:
        if self.error is not None:
            raise self.error
        return SalesAgentTurnResponse(
            conversation_id=request.conversation_id,
            latest_inbound_message_id=request.latest_inbound_message_id,
            reply="Claro, te ayudo con tu cita.",
            outcome="continue",
            handoff=False,
        )


def _sales_token(session, organization_id=ORG):
    principal_id, headers = _credential(
        session,
        name=SALES_AGENT,
        principal_type="agent",
        profile="sales-agent-v0",
        organization_id=organization_id,
    )
    return principal_id, headers["Authorization"].removeprefix("Bearer ")


def _sales_client(maker, token, runtime):
    from sales_agent.api import create_app as create_sales_app

    settings = AgentSettings(
        backend_base_url="http://backend.test",
        backend_credential=token,
        agent_database_url="postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/unused",
        model="fake",
        recursion_limit=12,
        request_timeout_seconds=1.0,
    )
    app = create_sales_app(runtime=runtime, settings=settings)
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False)


def _turn(maker, token, runtime, conversation_id, message_id):
    with _sales_client(maker, token, runtime) as sales:
        return sales.post(
            "/sales-agent/turn",
            headers={"Authorization": f"Bearer {token}"},
            json={"conversation_id": conversation_id, "latest_inbound_message_id": message_id},
        )


def _runs(session, agent_key=None):
    session.expire_all()
    statement = select(AgentRun).order_by(AgentRun.id)
    if agent_key:
        statement = statement.where(AgentRun.agent_key == agent_key)
    rows = session.scalars(statement).all()
    session.rollback()
    return rows


def _feed(client, headers, **params):
    response = client.get("/activity", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _walk(client, headers, *, limit, between=None, **params):
    seen, cursor, i = [], None, 0
    while True:
        query = {"limit": limit, **params}
        if cursor:
            query["cursor"] = cursor
        page = _feed(client, headers, **query)
        seen.extend((item["source"], item["id"]) for item in page["items"])
        cursor = page["next_cursor"]
        if between is not None:
            between(i)
        i += 1
        if cursor is None:
            return seen


# --- 6. the feed tells agents from humans, by name ------------------------------


def test_feed_shows_agent_run_agent_proposal_and_human_approval_by_name(client, maker, session):
    _airy(session)
    lucia_id, lucia = _lucia(session)
    _overdue(session)
    started = _run(client, lucia)
    assert started.status_code == 201, started.text
    [proposal] = session.scalars(select(AgentProposal)).all()
    session.rollback()
    item = client.get(f"/agent/proposals/{proposal.id}", headers=lucia).json()
    assert _approve(client, lucia, item).status_code == 200

    sales_id, token = _sales_token(session)
    conversation = _conversation(session)
    message = _message(session, conversation)
    assert _turn(maker, token, FakeRuntime(), conversation.id, message.id).status_code == 200

    # A legacy system row (actor_id is the literal 'system') carrying PII-ish state.
    with session.begin():
        record_event(
            session,
            organization_id=ORG,
            entity_type="patient",
            entity_id="999",
            action="patient.created",
            after_state={"full_name": "Paciente Secreto Zeta", "amount": "987.65"},
        )

    page = _feed(client, lucia, limit=100)
    items = page["items"]
    keys = [(i["source"], i["id"]) for i in items]
    assert len(keys) == len(set(keys))
    assert all(i["source"] != "audit" or i["entity_type"] != "agent_run" for i in items)

    reception = [i for i in items if i["source"] == "agent_run" and i["agent_key"] == "reception"]
    assert len(reception) == 1
    assert reception[0]["actor_kind"] == "agent"
    assert reception[0]["actor_principal_id"] == sales_id
    assert reception[0]["actor_display_name"] == SALES_AGENT
    assert reception[0]["action"] == "agent_run.completed"

    cob_run = [i for i in items if i["source"] == "agent_run" and i["agent_key"] == "cobranza"]
    assert len(cob_run) == 1 and cob_run[0]["actor_kind"] == "human"

    proposal_rows = {i["action"]: i for i in items if i["source"] == "proposal"}
    created = proposal_rows["agent_proposal.created"]
    assert created["actor_kind"] == "agent" and created["actor_display_name"] == "airy-cobranza"
    assert created["agent_key"] == "cobranza"
    approved = proposal_rows["agent_proposal.approved"]
    assert approved["actor_kind"] == "human"
    assert approved["actor_principal_id"] == lucia_id
    assert approved["actor_display_name"] == "Lucía Ramos"
    assert "Lucía Ramos" in approved["summary"]
    assert "agent_proposal.executed" in proposal_rows

    system = [i for i in items if i["action"] == "patient.created" and i["entity_id"] == "999"]
    assert system[0]["actor_kind"] == "system"
    assert system[0]["actor_display_name"] == "Sistema"
    assert system[0]["actor_principal_id"] is None
    text = client.get("/activity", params={"limit": 100}, headers=lucia).text
    assert "Paciente Secreto Zeta" not in text and "987.65" not in text
    assert all("after_state" not in i and "before_state" not in i for i in items)
    # The reception turn writes no audit row (agent_runs is the record).
    assert session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_type == "agent_run", AuditEvent.actor_id == str(sales_id)
        )
    ) == 0
    session.rollback()


# --- 7. filters, cursor, isolation ----------------------------------------------


def _agent_ctx(principal_id):
    return ExecutionContext(
        organization_id=ORG,
        principal_id=principal_id,
        principal_type="agent",
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
    )


def test_feed_filters_cursor_and_isolation(client, maker, session):
    _lucia_id, lucia = _lucia(session)
    sales_id, token = _sales_token(session)
    ids = seed_booking(session, suffix="b3-feed")
    other = seed_booking(session, suffix="b3-feed-2")
    book(session, ids)  # audit 'appointment.created' at ids' location (system actor)
    book(session, other)
    conversation = _conversation(session, lead_id=ids["lead_id"])
    message = _message(session, conversation)
    assert _turn(maker, token, FakeRuntime(), conversation.id, message.id).status_code == 200

    appointment_proposal = AppointmentProposal(
        organization_id=ORG,
        conversation_id=conversation.id,
        contact_identity_id=conversation.contact_identity_id,
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"],
        full_name="Paciente b3-feed",
        start_utc=datetime(2027, 1, 4, 15, tzinfo=UTC),
        end_utc=datetime(2027, 1, 4, 15, 30, tzinfo=UTC),
        confirmation_token=uuid4(),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session.add(appointment_proposal)
    session.flush()
    record_event(
        session,
        ctx=_agent_ctx(sales_id),
        entity_type="appointment_proposal",
        entity_id=str(appointment_proposal.id),
        action="appointment_proposal.created",
        after_state={"status": "pending"},
    )
    session.commit()

    other_org = _other_org(session, "Clinica B3 feed")
    book(session, seed_booking(session, organization_id=other_org, suffix="b3-feed-org-b"))

    everything = _feed(client, lucia, limit=100)["items"]
    assert all(i["entity_id"] != "b3-org-b" for i in everything)
    org_b_audit = session.scalars(
        select(AuditEvent.id).where(AuditEvent.organization_id == other_org)
    ).all()
    session.rollback()
    assert org_b_audit
    assert not {i["id"] for i in everything if i["source"] == "audit"} & set(org_b_audit)

    reception = _feed(client, lucia, agent_key="reception", limit=100)["items"]
    assert {(i["source"], i["action"]) for i in reception} == {
        ("agent_run", "agent_run.completed"),
        ("proposal", "appointment_proposal.created"),
    }
    assert all(i["agent_key"] == "reception" for i in reception)
    proposal_row = next(i for i in reception if i["source"] == "proposal")
    assert proposal_row["location_id"] == ids["location_id"]
    assert proposal_row["actor_kind"] == "agent"
    assert _feed(client, lucia, agent_key="cobranza")["items"] == []

    at_location = _feed(client, lucia, location_id=ids["location_id"], limit=100)["items"]
    assert at_location
    assert all(i["location_id"] == ids["location_id"] for i in at_location)
    assert {i["entity_type"] for i in at_location} == {"appointment", "appointment_proposal"}
    assert not [i for i in at_location if i["source"] == "agent_run"]

    since = datetime.now(UTC)
    later = conversation  # reuse: a second turn after ``since``
    msg2 = _message(session, later, text="otra", occurred_at=datetime.now(UTC))
    assert _turn(maker, token, FakeRuntime(), later.id, msg2.id).status_code == 200
    recent = _feed(client, lucia, since=since.isoformat(), limit=100)["items"]
    assert recent and all(
        datetime.fromisoformat(i["occurred_at"]) >= since for i in recent
    )
    naive = client.get("/activity", params={"since": "2026-09-01T00:00:00"}, headers=lucia)
    assert naive.status_code == 422
    for params in ({"cursor": "bad"}, {"agent_key": "x" * 200}, {"limit": 0}, {"foo": "1"}):
        assert client.get("/activity", params=params, headers=lucia).status_code == 422

    original = [(i["source"], i["id"]) for i in _feed(client, lucia, limit=100)["items"]]

    def between(i):
        with session.begin():
            record_event(
                session,
                ctx=default_context(ORG),
                entity_type="lead",
                entity_id=f"new-{i}",
                action="lead.created",
            )

    seen = _walk(client, lucia, limit=3, between=between)
    assert len(seen) == len(set(seen))
    assert [k for k in seen if k in original] == original


# --- 8. reception turn → one agent_runs row -------------------------------------------


def test_reception_turn_persists_one_run_on_every_path(maker, session, monkeypatch):
    sales_id, token = _sales_token(session)
    conversation = _conversation(session)
    message = _message(session, conversation)

    ok = _turn(maker, token, FakeRuntime(), conversation.id, message.id)
    assert ok.status_code == 200, ok.text
    [run] = _runs(session, "reception")
    assert (run.agent_key, run.trigger, run.status) == ("reception", "event", "completed")
    assert run.conversation_id == conversation.id
    assert run.trigger_message_id == message.id
    assert run.triggered_by_principal_id == sales_id
    assert run.finished_at >= run.started_at
    assert run.error_category is None
    assert (run.candidates_count, run.proposed_count) == (0, 0)

    failed = _turn(maker, token, FakeRuntime(RuntimeError("boom")), conversation.id, message.id)
    assert failed.status_code == 503
    run = _runs(session, "reception")[-1]
    assert run.status == "failed" and run.error_category

    from sales_agent import api as sales_api

    def _no_runtime(_settings):
        raise AgentUnavailableError("not installed")

    monkeypatch.setattr(sales_api, "_build_runtime", _no_runtime)
    unavailable = _turn(maker, token, None, conversation.id, message.id)
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "AGENT_UNAVAILABLE"
    run = _runs(session, "reception")[-1]
    assert (run.status, run.error_category) == ("failed", "agent_unavailable")
    monkeypatch.undo()
    count = len(_runs(session, "reception"))

    other = _conversation(session)
    foreign_message = _message(session, other)
    mismatched = _turn(maker, token, FakeRuntime(), conversation.id, foreign_message.id)
    assert mismatched.status_code == 200
    assert mismatched.json() == ok.json() | {"latest_inbound_message_id": foreign_message.id}
    assert len(_runs(session, "reception")) == count

    def _boom(*_args, **_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(sales_api, "record_reception_turn", _boom)
    forced = _turn(maker, token, FakeRuntime(), conversation.id, message.id)
    assert forced.status_code == 200
    assert forced.json() == ok.json()
    assert len(_runs(session, "reception")) == count

    # COB's run history still renders with reception rows present.
    from app.agents_runtime.service import list_runs

    page = list_runs(
        session, ctx=default_context(ORG), agent_key=None, limit=50
    )
    assert {item.agent_key for item in page.items} == {"reception"}
