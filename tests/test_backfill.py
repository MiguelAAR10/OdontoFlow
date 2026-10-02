"""BACKFILL — cancellation backfill agent: ``waitlist_offer`` proposals and approval.

Spec: ``docs/superpowers/specs/2026-10-01-erp-backfill.md``. Real PostgreSQL,
one pytest process. A cancelled appointment tomorrow at 10:00 becomes one
``waitlist_offer`` for the 3 oldest reachable matching entries; a human with
``deliveries.create`` + ``waitlist.manage`` approves, the entries become
``offered`` and one sandbox message per entry is queued, exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from test_agent_proposals import (
    _approve,
    _count,
    _credential,
    _decline,
    _human_with,
    _lucia,
)
from test_domain_gaps_helpers import seed_booking

from app.agent_jobs.models import AgentJob
from app.agents_runtime.models import AgentRun
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.clinical.models import Patient
from app.commercial.models import Lead
from app.context import default_context
from app.errors import AppError
from app.events.models import DomainEvent
from app.iam.models import Principal
from app.messaging.models import ChannelAccount, ContactIdentity, Conversation, OutboundMessage
from app.proposals.models import AgentProposal
from app.scheduling.models import Appointment
from app.scheduling.service import cancel_appointment
from app.scheduling.waitlist import WaitlistEntry, cancel_waitlist_entry, offer_waitlist_entries
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import _assign_profile, _resolve_principal

LIMA = ZoneInfo("America/Lima")
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


# --- scenario -----------------------------------------------------------------

_N = iter(range(1, 100_000))


def _tomorrow_at(hour: int) -> datetime:
    tomorrow = datetime.now(LIMA).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(hour), LIMA)


def _reachable_patient(session, organization_id, channel_id, *, consent="opted_in"):
    n = next(_N)
    lead = Lead(organization_id=organization_id, full_name=f"Espera {n}",
                contact_phone=f"+51955{n:06d}", acquisition_source="direct")
    patient = Patient(organization_id=organization_id, full_name=f"Espera {n}",
                      dni=f"81{n:06d}")
    session.add_all([lead, patient])
    session.flush()
    contact = ContactIdentity(organization_id=organization_id, channel_account_id=channel_id,
                              external_contact_id=f"bf-{n}",
                              normalized_phone_e164=f"+51955{n:06d}",
                              patient_id=patient.id, consent_status=consent)
    session.add(contact)
    session.flush()
    conversation = Conversation(organization_id=organization_id, channel_account_id=channel_id,
                                contact_identity_id=contact.id, status="open",
                                last_message_at=datetime.now(UTC))
    session.add(conversation)
    session.flush()
    return lead.id, patient.id, conversation.id


def _entry(session, ids, *, lead_id, patient_id, minutes_ago, service_id=None,
           location="same", window="morning") -> int:
    today = datetime.now(LIMA).date()
    row = WaitlistEntry(
        organization_id=ids["organization_id"],
        lead_id=lead_id,
        patient_id=patient_id,
        service_id=service_id or ids["service_id"],
        location_id=ids["location_id"] if location == "same" else None,
        earliest_date=today,
        latest_date=today + timedelta(days=7),
        preferred_window=window,
        status="open",
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    session.add(row)
    session.flush()
    return row.id


def _scenario(session, *, organization_id=ORG):
    """Tomorrow 10:00 confirmed appointment + 4 matching reachable entries + 4 decoys."""
    n = next(_N)
    ids = seed_booking(session, organization_id=organization_id, suffix=f"bf-{n}")
    start = _tomorrow_at(10)
    appointment = Appointment(
        organization_id=organization_id, lead_id=ids["lead_id"], service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"], location_id=ids["location_id"],
        start_utc=start.astimezone(UTC), end_utc=(start + timedelta(minutes=30)).astimezone(UTC),
        state="confirmed",
    )
    other_service = Service(organization_id=organization_id, name=f"Ortodoncia bf-{n}",
                            duration_minutes=30, base_price=Decimal("90.00"), currency="PEN",
                            is_active=True)
    channel = ChannelAccount(organization_id=organization_id, provider="sandbox",
                             external_account_id=f"sandbox-bf-{n}", phone_number_id=None,
                             display_name="sandbox", is_active=True)
    session.add_all([appointment, other_service, channel])
    session.flush()

    # Decoys (older than every matching entry, so ranking must skip them).
    lead, patient, _c = _reachable_patient(session, organization_id, channel.id, consent="opted_out")
    opted_out = _entry(session, ids, lead_id=lead, patient_id=patient, minutes_ago=100)
    lead, patient, _c = _reachable_patient(session, organization_id, channel.id)
    wrong_service = _entry(session, ids, lead_id=lead, patient_id=patient, minutes_ago=99,
                           service_id=other_service.id)
    lead, patient, _c = _reachable_patient(session, organization_id, channel.id)
    wrong_window = _entry(session, ids, lead_id=lead, patient_id=patient, minutes_ago=98,
                          window="afternoon")
    lead, _patient, _c = _reachable_patient(session, organization_id, channel.id)
    lead_only = _entry(session, ids, lead_id=lead, patient_id=None, minutes_ago=97)

    matching, conversations = [], {}
    for minutes_ago, location, window in ((50, "same", "morning"), (40, "any", "any"),
                                          (30, "same", "any"), (20, "same", "morning")):
        lead, patient, conversation = _reachable_patient(session, organization_id, channel.id)
        entry_id = _entry(session, ids, lead_id=lead, patient_id=patient,
                          minutes_ago=minutes_ago, location=location, window=window)
        matching.append(entry_id)
        conversations[entry_id] = conversation
    session.commit()
    return {
        "ids": ids,
        "appointment_id": appointment.id,
        "start": start,
        "matching": matching,
        "conversations": conversations,
        "decoys": [opted_out, wrong_service, wrong_window, lead_only],
        "service_name": f"Limpieza bf-{n}",
        "location_name": f"Sede bf-{n}",
    }


def _cancel(session, scenario):
    cancel_appointment(session, scenario["appointment_id"],
                       ctx=default_context(scenario["ids"]["organization_id"]))
    session.rollback()


def _airy(session, organization_id=ORG) -> int:
    principal = _resolve_principal(session, organization_id=organization_id, name=AIRY,
                                   principal_type="agent")
    _assign_profile(session, organization_id=organization_id, principal_id=principal.id,
                    profile="backfill-agent")
    session.commit()
    return principal.id


def _tick(client, headers):
    response = client.post("/agent-runs/jobs/run-due", json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _offers(session):
    session.expire_all()
    rows = session.scalars(
        select(AgentProposal).where(AgentProposal.kind == "waitlist_offer").order_by(AgentProposal.id)
    ).all()
    for row in rows:
        session.expunge(row)  # detached + loaded: reading it never re-begins the session
    session.rollback()
    return rows


def _item(client, headers, proposal_id):
    response = client.get(f"/agent/proposals/{proposal_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _statuses(session, entry_ids):
    session.expire_all()
    rows = dict(session.execute(
        select(WaitlistEntry.id, WaitlistEntry.status).where(WaitlistEntry.id.in_(entry_ids))
    ).all())
    session.rollback()
    return [rows[i] for i in entry_ids]


def _proposed(client, session):
    """Cancel → tick as Lucía (airy-backfill proposes) → the one offer item."""
    scenario = _scenario(session)
    airy_id = _airy(session)
    _lid, lucia = _lucia(session)
    _cancel(session, scenario)
    body = _tick(client, lucia)
    assert (body["enqueued"], body["claimed"], body["done"]) == (1, 1, 1), body
    [offer] = _offers(session)
    return scenario, airy_id, lucia, body, _item(client, lucia, offer.id)


# --- 6. demo --------------------------------------------------------------------


def test_demo_cancellation_tomorrow_proposes_one_offer_to_the_three_oldest(client, session):
    scenario, airy_id, lucia, body, item = _proposed(client, session)
    first_three = scenario["matching"][:3]
    [job] = body["jobs"]
    assert job["status"] == "done" and job["agent_key"] == "backfill"

    assert item["kind"] == "waitlist_offer" and item["agent_key"] == "backfill"
    assert item["status"] == "pending" and item["actions"] == ["approve", "decline"]
    assert item["subject"] == {"type": "appointment", "id": str(scenario["appointment_id"])}
    assert item["location_id"] == scenario["ids"]["location_id"]
    assert item["summary"] == (
        f"Cupo libre — ofrecer a 3 pacientes en lista de espera "
        f"(cita #{scenario['appointment_id']})"
    )
    start = scenario["start"]
    text_body = (
        f"Hola, se liberó un cupo de {scenario['service_name']} el {start:%d/%m} a las "
        f"{start:%H:%M} en {scenario['location_name']}. Responde SÍ en los próximos 30 "
        "minutos para reservarlo."
    )
    assert item["payload"] == {"appointment_id": scenario["appointment_id"],
                               "entry_ids": first_three, "message_text": text_body}
    evidence = item["evidence"]
    assert evidence == {
        "run_id": job["run_id"],
        "job_key": job["job_key"],
        "appointment_id": scenario["appointment_id"],
        "start_utc": start.astimezone(UTC).isoformat(),
        "service": scenario["service_name"],
        "location": scenario["location_name"],
        "matched": 4,
        "offered_to": first_three,
    }

    row = _offers(session)[0]
    assert row.proposed_by_principal_id == airy_id
    assert row.subject_version == "free|" + ",".join(str(i) for i in sorted(first_three))
    expires_in = row.expires_at - row.created_at
    assert timedelta(minutes=29) < expires_in <= timedelta(minutes=30)

    run = session.get(AgentRun, job["run_id"])
    lucia_id = session.scalar(select(Principal.id).where(Principal.display_name == "Lucía Ramos"))
    assert (run.agent_key, run.trigger, run.status) == ("backfill", "event", "completed")
    assert run.triggered_by_principal_id == lucia_id
    # Counts are per freed slot (ck_agent_runs_counts); ``matched`` is in the evidence.
    assert (run.candidates_count, run.proposed_count) == (1, 1)
    session.rollback()

    listed = client.get("/agent-runs?agent_key=backfill", headers=lucia)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [job["run_id"]]
    assert _statuses(session, scenario["matching"] + scenario["decoys"]) == ["open"] * 8


def test_far_or_unmatched_cancellations_propose_nothing(client, session):
    scenario = _scenario(session)
    _pid, agent = _credential(session, name="airy-backfill-n8n", principal_type="agent",
                              profile="backfill-agent")
    session.execute(text("UPDATE waitlist_entries SET status = 'cancelled' WHERE id = ANY(:ids)"),
                    {"ids": scenario["matching"]})
    session.commit()
    _cancel(session, scenario)
    body = _tick(client, agent)
    assert body["done"] == 1
    assert _offers(session) == []
    run = session.get(AgentRun, body["jobs"][0]["run_id"])
    assert run.status == "completed" and run.candidates_count == 0
    session.rollback()


def test_a_slot_already_rebooked_is_skipped(client, session):
    scenario = _scenario(session)
    _pid, agent = _credential(session, name="airy-backfill-n8n", principal_type="agent",
                              profile="backfill-agent")
    _cancel(session, scenario)
    _rebook(session, scenario)
    body = _tick(client, agent)
    assert body["done"] == 1 and _offers(session) == []
    run = session.get(AgentRun, body["jobs"][0]["run_id"])
    assert run.skipped_count == 1
    session.rollback()


# --- 7. dedupe -------------------------------------------------------------------


def _requeue(session):
    session.execute(text("UPDATE agent_jobs SET status = 'queued', run_after = now()"))
    session.commit()


def test_repeated_ticks_and_requeued_jobs_never_duplicate_the_offer(client, session):
    _scenario_, _airy_id, lucia, _body, item = _proposed(client, session)
    again = _tick(client, lucia)
    assert (again["enqueued"], again["claimed"]) == (0, 0)
    assert _count(session, AgentJob) == 1

    _requeue(session)
    requeued = _tick(client, lucia)
    assert requeued["claimed"] == 1 and requeued["done"] == 1
    run = session.get(AgentRun, requeued["jobs"][0]["run_id"])
    assert run.deduped_count == 1 and run.proposed_count == 0
    session.rollback()
    assert len(_offers(session)) == 1

    assert _decline(client, lucia, item["id"]).status_code == 200
    _requeue(session)
    assert _tick(client, lucia)["done"] == 1
    assert len(_offers(session)) == 1


# --- 8. approval -------------------------------------------------------------------


def test_lucia_approves_and_three_patients_get_the_offer_once(client, session):
    scenario, _airy_id, lucia, _body, item = _proposed(client, session)
    first_three = scenario["matching"][:3]
    key = str(uuid4())
    response = _approve(client, lucia, item, key=key)
    assert response.status_code == 200, response.text
    approved = response.json()
    assert approved["status"] == "executed"
    assert _statuses(session, scenario["matching"]) == ["offered"] * 3 + ["open"]
    assert _statuses(session, scenario["decoys"]) == ["open"] * 4

    messages = session.scalars(select(OutboundMessage).order_by(OutboundMessage.id)).all()
    assert len(messages) == 3
    assert {m.conversation_id for m in messages} == {
        scenario["conversations"][i] for i in first_three
    }
    assert {m.payload["text"] for m in messages} == {item["payload"]["message_text"]}
    assert approved["result_ref"] == {"type": "waitlist_offer", "entry_ids": first_three,
                                      "outbound_ids": [m.id for m in messages]}
    session.rollback()

    assert _count(session, AuditEvent, AuditEvent.action == "waitlist_entry.offered") == 3
    events = session.scalars(
        select(DomainEvent).where(DomainEvent.event_type == "waitlist.offered")
        .order_by(DomainEvent.id)
    ).all()
    assert [e.payload["entry_id"] for e in events] == first_three
    assert {e.payload["proposal_id"] for e in events} == {item["id"]}
    assert {e.payload["appointment_id"] for e in events} == {scenario["appointment_id"]}
    session.rollback()

    replay = _approve(client, lucia, item, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert _count(session, OutboundMessage) == 3


# --- 9. drift and permissions ----------------------------------------------------------


def _rebook(session, scenario):
    ids = scenario["ids"]
    start = scenario["start"]
    session.add(Appointment(
        organization_id=ids["organization_id"], lead_id=ids["lead_id"],
        service_id=ids["service_id"], practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"], start_utc=start.astimezone(UTC),
        end_utc=(start + timedelta(minutes=30)).astimezone(UTC), state="confirmed",
    ))
    session.commit()


def test_a_rebooked_slot_supersedes_the_offer(client, session):
    scenario, _airy_id, lucia, _body, item = _proposed(client, session)
    _rebook(session, scenario)
    response = _approve(client, lucia, item)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_SUPERSEDED"
    assert _count(session, OutboundMessage) == 0
    assert _statuses(session, scenario["matching"]) == ["open"] * 4


def test_a_cancelled_entry_supersedes_the_offer(client, session):
    scenario, _airy_id, lucia, _body, item = _proposed(client, session)
    cancel_waitlist_entry(session, scenario["matching"][1], organization_id=ORG)
    session.rollback()
    response = _approve(client, lucia, item)
    assert response.status_code == 409, response.text
    assert _count(session, OutboundMessage) == 0


def test_approvers_need_deliveries_create_and_waitlist_manage(client, session):
    from app.iam.permissions import DELIVERIES_CREATE, PROPOSALS_DECIDE, PROPOSALS_READ

    scenario, _airy_id, _lucia_h, _body, item = _proposed(client, session)
    _hid, no_deliveries = _human_with(session, (PROPOSALS_READ, PROPOSALS_DECIDE),
                                      name="Sin Envios")
    assert _approve(client, no_deliveries, item).status_code == 403
    assert _offers(session)[0].status == "pending"

    _hid, no_waitlist = _human_with(session, (PROPOSALS_READ, PROPOSALS_DECIDE, DELIVERIES_CREATE),
                                    name="Sin Lista")
    response = _approve(client, no_waitlist, item)
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["error_code"]) == (
        "failed", "PERMISSION_DENIED")
    assert _statuses(session, scenario["matching"]) == ["open"] * 4
    assert _count(session, OutboundMessage) == 0


def test_offer_rechecks_the_slot_and_the_tenant_at_execute(client, session):
    scenario, _airy_id, _lucia_h, _body, item = _proposed(client, session)
    lucia_ctx = _lucia_ctx(session)
    _rebook(session, scenario)
    with pytest.raises(AppError) as excinfo:
        offer_waitlist_entries(session, lucia_ctx, entry_ids=item["payload"]["entry_ids"],
                               proposal_id=item["id"], appointment_id=scenario["appointment_id"])
    assert excinfo.value.code.value == "INVALID_INPUT"
    session.rollback()
    assert _statuses(session, scenario["matching"]) == ["open"] * 4


def test_a_slot_rebooked_after_approval_fails_the_offer_and_sends_nothing(
    client, session, maker, monkeypatch
):
    """The slot is taken between tx1 (version + revalidate pass) and the executor."""
    from dataclasses import replace

    from app.proposals.executors import KINDS

    scenario, _airy_id, lucia, _body, item = _proposed(client, session)
    spec = KINDS["waitlist_offer"]

    def rebook_after_the_version_check(db, organization_id, args):
        spec.revalidate(db, organization_id, args)
        other = maker()
        try:
            _rebook(other, scenario)
        finally:
            other.close()

    monkeypatch.setitem(KINDS, "waitlist_offer",
                        replace(spec, revalidate=rebook_after_the_version_check))
    response = _approve(client, lucia, item)
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["error_code"]) == (
        "failed", "INVALID_INPUT")
    assert _statuses(session, scenario["matching"]) == ["open"] * 4
    assert _count(session, OutboundMessage) == 0
    assert _count(session, DomainEvent, DomainEvent.event_type == "waitlist.offered") == 0


def _lucia_ctx(session):
    from app.iam.context import ExecutionContext

    principal_id = session.scalar(
        select(Principal.id).where(Principal.display_name == "Lucía Ramos")
    )
    session.rollback()
    return ExecutionContext(organization_id=ORG, principal_id=principal_id,
                            principal_type="human", request_id="r", correlation_id="c")


def test_offer_ignores_another_tenants_entry_ids(client, session):
    from test_agent_proposals import _other_org

    scenario, _airy_id, _lucia_h, _body, item = _proposed(client, session)
    org_b = _other_org(session, name="Clinica BF B")
    foreign = _scenario(session, organization_id=org_b)
    with pytest.raises(AppError) as excinfo:
        offer_waitlist_entries(session, _lucia_ctx(session), entry_ids=foreign["matching"][:3],
                               proposal_id=item["id"], appointment_id=scenario["appointment_id"])
    assert excinfo.value.code.value == "INVALID_INPUT"
    session.rollback()
    assert _statuses(session, foreign["matching"]) == ["open"] * 4


# --- 10. HTTP refusals ---------------------------------------------------------------


def test_waitlist_offer_is_never_proposed_over_http(client, session):
    _pid, agent = _credential(session, name="airy-backfill-n8n", principal_type="agent",
                              profile="backfill-agent")
    response = client.post(
        "/agent/proposals",
        json={"kind": "waitlist_offer", "reason": "cupo",
              "payload": {"appointment_id": 1, "entry_ids": [1], "message_text": "hola"}},
        headers={**agent, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422, response.text
    assert _count(session, AgentProposal) == 0


def test_backfill_runs_are_never_started_by_post_agent_runs(client, session):
    _pid, lucia = _lucia(session)
    response = client.post("/agent-runs", json={"agent_key": "backfill"},
                           headers={**lucia, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 422, response.text
    assert _count(session, AgentRun) == 0


def test_waitlist_offer_requires_deliveries_create_and_never_l4():
    from app.iam.permissions import DELIVERIES_CREATE, PAYMENTS_MANAGE, PAYMENTS_REVERSE
    from app.proposals.executors import KINDS, OFFER_TTL

    spec = KINDS["waitlist_offer"]
    assert spec.required_permission == DELIVERIES_CREATE
    assert spec.required_permission not in {PAYMENTS_REVERSE, PAYMENTS_MANAGE}
    assert spec.ttl == OFFER_TTL == timedelta(minutes=30)


def test_backfill_profile_only_proposes_and_reads():
    from scripts.issue_credential import PROFILE_PERMISSIONS

    assert set(PROFILE_PERMISSIONS["backfill-agent"]) == {
        "proposals.create", "proposals.read", "appointments.read", "waitlist.read",
    }
    assert func  # keep the import used by helpers above
