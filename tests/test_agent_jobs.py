"""BACKFILL — ``agent_jobs``: enqueue from domain events, lease + fencing claim,
settle, kill switch, the ``run-due`` tick route and its CLI command.

Spec: ``docs/superpowers/specs/2026-10-01-erp-backfill.md``. Real PostgreSQL,
one pytest process. The concurrency test holds worker A's claim transaction
open (thread + ``Event``) while worker B claims, so the overlap of the two
``FOR UPDATE SKIP LOCKED`` statements is deterministic, not timing-based.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_agent_proposals import _credential, _lucia, _other_org

from app.agent_jobs.models import AgentJob
from app.agent_jobs.service import (
    LeaseLost,
    claim_in_tx,
    claim_one,
    enqueue_from_events,
    run_due,
    settle,
)
from app.context import default_context
from app.errors import AppError
from app.events.models import DomainEvent
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import _assign_profile, _resolve_principal

KEYS = ["backfill"]
PROFILE = "backfill-agent"
AIRY = "airy-backfill"


@pytest.fixture
def maker(migrated_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client(maker):
    from fastapi.testclient import TestClient

    from app import create_app
    from app.db import get_db

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
    monkeypatch.delenv("AGENT_BACKFILL_ENABLED", raising=False)
    monkeypatch.delenv("ODONTOFLOW_TOKEN", raising=False)
    monkeypatch.delenv("ODONTOFLOW_URL", raising=False)


# --- helpers ------------------------------------------------------------------


def _event(session, *, organization_id=ORG, event_type="appointment.cancelled",
           start_in=timedelta(hours=20), appointment_id=987654) -> int:
    """A cancellation fact whose appointment does not exist (the handler skips it)."""
    start = datetime.now(UTC) + start_in
    row = DomainEvent(
        organization_id=organization_id,
        event_type=event_type,
        aggregate_type="appointment",
        aggregate_id=str(appointment_id),
        payload={"appointment_id": appointment_id, "start_utc": start.isoformat(),
                 "end_utc": (start + timedelta(minutes=30)).isoformat()},
    )
    session.add(row)
    session.commit()
    return row.id


def _job(session, event_id=None, *, organization_id=ORG) -> int:
    event_id = event_id or _event(session, organization_id=organization_id)
    enqueue_from_events(session, default_context(organization_id))
    job_id = session.scalar(
        select(AgentJob.id).where(AgentJob.organization_id == organization_id,
                                  AgentJob.source_event_id == event_id)
    )
    session.rollback()
    return job_id


def _row(session, job_id) -> AgentJob:
    session.expire_all()
    row = session.get(AgentJob, job_id)
    session.expunge(row)  # detached + loaded: reading it never re-begins the session
    session.rollback()
    return row


def _jobs(session, organization_id=ORG):
    session.expire_all()
    rows = session.scalars(
        select(AgentJob).where(AgentJob.organization_id == organization_id).order_by(AgentJob.id)
    ).all()
    for row in rows:
        session.expunge(row)
    session.rollback()
    return rows


def _expire_lease(session, job_id, *, attempts=None):
    extra = ", attempts = :attempts" if attempts is not None else ""
    session.execute(
        text(f"UPDATE agent_jobs SET leased_until = now() - interval '1 second'{extra} "
             "WHERE id = :id"),
        {"id": job_id, "attempts": attempts},
    )
    session.commit()


def _agent(session, organization_id=ORG, name="airy-backfill-n8n"):
    return _credential(session, name=name, principal_type="agent", profile=PROFILE,
                       organization_id=organization_id)


def _airy(session, organization_id=ORG) -> int:
    principal = _resolve_principal(session, organization_id=organization_id, name=AIRY,
                                   principal_type="agent")
    _assign_profile(session, organization_id=organization_id, principal_id=principal.id,
                    profile=PROFILE)
    session.commit()
    return principal.id


def _tick(client, headers, body=None):
    return client.post("/agent-runs/jobs/run-due", json=body if body is not None else {},
                       headers=headers)


# --- 1. deterministic mutual exclusion ------------------------------------------


def _hold_and_race(maker):
    """A claims inside an open tx (row lock held); B claims meanwhile; then A commits."""
    holder, racer = maker(), maker()
    held, release = threading.Event(), threading.Event()
    result: dict = {}

    def worker_a():
        try:
            with holder.begin():
                result["a"] = claim_in_tx(holder, ORG, KEYS)
                held.set()
                assert release.wait(10)
        except BaseException as exc:  # surface thread errors to the test
            result["error"] = exc
            held.set()

    thread = threading.Thread(target=worker_a)
    thread.start()
    try:
        assert held.wait(10)
        result["b"] = claim_one(racer, ORG, KEYS)
    finally:
        release.set()
        thread.join(10)
        holder.close()
        racer.close()
    assert "error" not in result, result.get("error")
    return result["a"], result["b"]


def test_two_workers_never_claim_the_same_job(maker, session):
    only = _job(session)
    first, second = _hold_and_race(maker)
    assert first is not None and first.id == only
    assert second is None
    row = _row(session, only)
    assert row.status == "leased" and row.attempts == 1 and row.lease_token == first.lease_token


def test_two_workers_over_two_jobs_get_distinct_ids(maker, session):
    ids = {_job(session), _job(session)}
    first, second = _hold_and_race(maker)
    assert first is not None and second is not None
    assert {first.id, second.id} == ids
    assert first.lease_token != second.lease_token


# --- 2. lease expiry, fencing and the dead sweep ----------------------------------


def test_expired_lease_is_reclaimed_and_the_stale_settle_is_fenced(maker, session):
    job_id = _job(session)
    worker_a, worker_b = maker(), maker()
    try:
        a = claim_one(worker_a, ORG, KEYS)
        _expire_lease(session, job_id)
        b = claim_one(worker_b, ORG, KEYS)
        assert b.id == a.id == job_id
        assert b.lease_token != a.lease_token and b.attempts == 2

        with pytest.raises(LeaseLost):
            settle(worker_a, ORG, job_id, a.lease_token, ok=True)
        row = _row(session, job_id)
        assert row.status == "leased" and row.lease_token == b.lease_token

        assert settle(worker_b, ORG, job_id, b.lease_token, ok=True) == "done"
        row = _row(session, job_id)
        assert row.status == "done" and row.lease_token is None and row.leased_until is None
    finally:
        worker_a.close()
        worker_b.close()


def test_an_expired_lease_at_max_attempts_goes_dead_and_the_sweep_is_scoped(session):
    org_b = _other_org(session, name="Clinica Jobs B")
    mine = _job(session)
    theirs = _job(session, organization_id=org_b)
    for job_id, org in ((mine, ORG), (theirs, org_b)):
        claim_one(session, org, KEYS)
        _expire_lease(session, job_id, attempts=3)

    # A disabled agent's rows are never swept (no keys → no sweep, no claim).
    assert claim_one(session, ORG, []) is None
    assert _row(session, mine).status == "leased"

    assert claim_one(session, ORG, KEYS) is None
    row = _row(session, mine)
    assert row.status == "dead" and row.lease_token is None and row.leased_until is None
    assert row.last_error == "lease_expired"
    assert _row(session, theirs).status == "leased"  # org A's tick never kills org B's job


# --- 3. failure path ----------------------------------------------------------------


def test_failed_settles_back_off_and_the_third_failure_is_dead(session):
    job_id = _job(session)
    for attempt in (1, 2):
        claimed = claim_one(session, ORG, KEYS)
        assert claimed.attempts == attempt
        assert settle(session, ORG, job_id, claimed.lease_token, ok=False,
                      error="INVALID_INPUT") == "failed"
        row = _row(session, job_id)
        assert row.status == "failed" and row.last_error == "INVALID_INPUT"
        assert row.lease_token is None
        assert row.run_after > datetime.now(UTC) + timedelta(seconds=30)
        assert claim_one(session, ORG, KEYS) is None  # backing off
        session.execute(text("UPDATE agent_jobs SET run_after = now() WHERE id = :id"),
                        {"id": job_id})
        session.commit()
    claimed = claim_one(session, ORG, KEYS)
    assert settle(session, ORG, job_id, claimed.lease_token, ok=False) == "dead"
    row = _row(session, job_id)
    assert row.status == "dead" and row.attempts == 3 and row.last_error == "unexpected"


def test_a_handler_failure_fails_the_job_and_the_run(client, session, monkeypatch):
    from app.agents_runtime import backfill
    from app.agents_runtime.models import AgentRun

    def boom(*_args, **_kwargs):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(backfill, "_propose", boom)
    _event(session)
    _pid, agent = _agent(session)
    response = _tick(client, agent)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["claimed"] == 1 and body["failed"] == 1 and body["done"] == 0
    [job] = body["jobs"]
    assert job["status"] == "failed" and job["last_error"] == "unexpected"
    run = session.get(AgentRun, job["run_id"])
    assert run.status == "failed" and run.error_category == "unexpected"
    session.rollback()


# --- 4. kill switch -----------------------------------------------------------------


def test_kill_switch_stops_at_the_next_claim(client, session, monkeypatch):
    _event(session)
    _pid, agent = _agent(session)
    monkeypatch.setenv("AGENT_BACKFILL_ENABLED", "false")
    off = _tick(client, agent)
    assert off.status_code == 200, off.text
    assert off.json()["enqueued"] == 1 and off.json()["claimed"] == 0
    assert off.json()["disabled_agents"] == ["backfill"]
    [row] = _jobs(session)
    assert row.status == "queued" and row.attempts == 0

    monkeypatch.setenv("AGENT_BACKFILL_ENABLED", "true")
    on = _tick(client, agent).json()
    assert on["claimed"] == 1 and on["done"] == 1 and on["disabled_agents"] == []
    assert _jobs(session)[0].status == "done"


# --- 5. enqueue ------------------------------------------------------------------


def test_enqueue_is_one_job_per_event_and_only_near_cancellations(session):
    near = _event(session)
    _event(session, start_in=timedelta(days=3))
    _event(session, event_type="appointment.completed")
    ctx = default_context(ORG)
    assert enqueue_from_events(session, ctx) == 1
    assert enqueue_from_events(session, ctx) == 0
    [row] = _jobs(session)
    assert row.source_event_id == near and row.job_key == f"backfill:event:{near}"
    assert row.agent_key == "backfill" and row.status == "queued" and row.attempts == 0


def test_an_org_b_tick_never_sees_org_a_events_or_jobs(client, session):
    org_b = _other_org(session, name="Clinica Jobs C")
    _event(session)
    _pid, agent_b = _agent(session, organization_id=org_b, name="airy-backfill-b")
    body = _tick(client, agent_b).json()
    assert body["enqueued"] == 0 and body["claimed"] == 0 and body["jobs"] == []
    assert _jobs(session, org_b) == [] and _jobs(session) == []
    _pid, agent_a = _agent(session)
    assert _tick(client, agent_a).json()["enqueued"] == 1
    assert _tick(client, agent_b).json()["claimed"] == 0
    assert len(_jobs(session)) == 1


# --- 6. gate ----------------------------------------------------------------------


def test_gate_refuses_missing_reads_and_system(client, session):
    _pid, collections = _credential(session, name="airy-cobranza-x", principal_type="agent",
                                    profile="collections-agent")
    assert _tick(client, collections).status_code == 403
    with pytest.raises(AppError) as excinfo:
        run_due(session, ctx=default_context(ORG), limit=10)
    assert excinfo.value.code.value == "PERMISSION_DENIED"


def test_a_human_tick_without_the_backfill_proposer_leaves_the_job_untouched(client, session):
    _event(session)
    _pid, lucia = _lucia(session)
    refused = _tick(client, lucia)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "AGENT_DISABLED"
    assert refused.json()["error"]["details"]["reason"] == "not_provisioned"
    [row] = _jobs(session)
    assert row.status == "queued" and row.attempts == 0 and row.lease_token is None

    _airy(session)
    body = _tick(client, lucia).json()
    assert body["claimed"] == 1 and body["done"] == 1


def test_a_human_tick_with_nothing_due_needs_no_proposer(client, session):
    _pid, lucia = _lucia(session)
    response = _tick(client, lucia)
    assert response.status_code == 200, response.text
    assert response.json()["claimed"] == 0


def test_body_validation(client, session):
    _pid, agent = _agent(session)
    for body in ({"limit": 0}, {"limit": 21}, {"limit": 5, "agent_key": "backfill"}):
        assert _tick(client, agent, body).status_code == 422, body
    for _ in range(3):
        _event(session)
    body = _tick(client, agent, {"limit": 2}).json()
    assert body["enqueued"] == 3 and body["claimed"] == 2
    assert _tick(client, agent).json()["claimed"] == 1


# --- 7. CHECKs -------------------------------------------------------------------


def test_checks_reject_bad_rows(session):
    event_id = _event(session)
    base = ("INSERT INTO agent_jobs (organization_id, agent_key, job_key, source_event_id, "
            "run_after, status, lease_token, leased_until) VALUES (:org, :agent, :key, :event, "
            "now(), :status, {token}, {until})")
    for agent, status, token, until in (
        ("backfill", "running", "NULL", "NULL"),
        ("cobranza", "queued", "NULL", "NULL"),
        ("backfill", "leased", "NULL", "now()"),
        ("backfill", "queued", "gen_random_uuid()", "now()"),
    ):
        with pytest.raises(IntegrityError):
            session.execute(text(base.format(token=token, until=until)),
                            {"org": ORG, "agent": agent, "key": f"k-{status}-{agent}-{token}",
                             "event": event_id, "status": status})
        session.rollback()


# --- 8. CLI -----------------------------------------------------------------------


def test_cli_jobs_run_due_posts_to_the_route(client, session, monkeypatch, capsys):
    import json

    from odontoflow_cli.main import main

    _event(session)
    _pid, agent = _agent(session)
    sent = []
    client.event_hooks = {"request": [sent.append], "response": []}
    monkeypatch.setenv("ODONTOFLOW_TOKEN", agent["Authorization"].removeprefix("Bearer "))
    assert main(["jobs", "run-due", "--limit", "3", "--json"], http_client=client) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claimed"] == 1 and out["done"] == 1
    [request] = [r for r in sent if r.url.path == "/agent-runs/jobs/run-due"]
    assert request.method == "POST" and json.loads(request.content) == {"limit": 3}

    assert main(["jobs", "run-due"], http_client=client) == 0
    assert "claimed=0" in capsys.readouterr().out
