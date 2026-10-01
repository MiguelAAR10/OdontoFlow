"""B3 — staff reads: conversations, messages, handoffs and the human claim.

Spec: ``docs/superpowers/specs/2026-10-01-erp-b3.md``. Real PostgreSQL, one
pytest process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import _carlos, _credential, _human_with, _lucia, _other_org

from app import create_app
from app.audit.models import AuditEvent
from app.clinical.models import Patient
from app.commercial.models import Lead
from app.db import get_db
from app.idempotency.models import CommandReceipt
from app.messaging.models import (
    ChannelAccount,
    ContactIdentity,
    Conversation,
    Message,
    ReceptionHandoff,
)
from app.organization.models import Location
from app.scheduling.models import Appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

BASE = datetime(2026, 9, 20, 15, tzinfo=UTC)


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


# --- helpers ------------------------------------------------------------------

_N = iter(range(1, 100_000))


def _channel(session, organization_id=ORG) -> ChannelAccount:
    n = next(_N)
    channel = ChannelAccount(
        organization_id=organization_id,
        provider="whatsapp",
        external_account_id=f"wa-b3-{n}",
        phone_number_id=f"phone-b3-{n}",
        display_name=f"WhatsApp b3 {n}",
        is_active=True,
    )
    session.add(channel)
    session.flush()
    return channel


def _conversation(
    session,
    *,
    organization_id=ORG,
    last_message_at=BASE,
    phone=None,
    lead_id=None,
    patient_id=None,
    status="open",
    channel=None,
) -> Conversation:
    n = next(_N)
    channel = channel or _channel(session, organization_id)
    contact = ContactIdentity(
        organization_id=organization_id,
        channel_account_id=channel.id,
        external_contact_id=f"contact-b3-{n}",
        normalized_phone_e164=phone or f"+5198{n:07d}",
        lead_id=lead_id,
        patient_id=patient_id,
        consent_status="opted_in",
    )
    session.add(contact)
    session.flush()
    conversation = Conversation(
        organization_id=organization_id,
        channel_account_id=channel.id,
        contact_identity_id=contact.id,
        status=status,
        last_message_at=last_message_at,
    )
    session.add(conversation)
    session.commit()
    return conversation


def _message(
    session,
    conversation: Conversation,
    *,
    text="Hola, quiero una cita",
    occurred_at=BASE,
    direction="inbound",
    message_type="text",
    redacted=False,
    expires_at=None,
    media=None,
) -> Message:
    message = Message(
        organization_id=conversation.organization_id,
        channel_account_id=conversation.channel_account_id,
        conversation_id=conversation.id,
        direction=direction,
        provider_message_id=f"wamid-{uuid4()}" if direction == "inbound" else None,
        message_type=message_type,
        body_text=None if redacted else text,
        media_reference=media,
        delivery_status="received" if direction == "inbound" else "sent",
        occurred_at=occurred_at,
        content_expires_at=expires_at or datetime.now(UTC) + timedelta(days=30),
        content_redacted_at=datetime.now(UTC) if redacted else None,
    )
    session.add(message)
    session.commit()
    return message


def _handoff(session, conversation: Conversation, *, status="pending", created_at=None):
    handoff = ReceptionHandoff(
        organization_id=conversation.organization_id,
        conversation_id=conversation.id,
        contact_identity_id=conversation.contact_identity_id,
        reason_code="requested_by_contact",
        reason_summary="El paciente pide hablar con una persona.",
        status=status,
        created_at=created_at or datetime.now(UTC),
    )
    session.add(handoff)
    conversation.status = "human_handoff" if status != "resolved" else conversation.status
    session.commit()
    return handoff


def _lead(session, name, organization_id=ORG) -> Lead:
    lead = Lead(
        organization_id=organization_id,
        full_name=name,
        contact_phone=f"+5199{next(_N):07d}",
        acquisition_source="direct",
    )
    session.add(lead)
    session.commit()
    return lead


def _patient(session, name, organization_id=ORG) -> Patient:
    patient = Patient(organization_id=organization_id, full_name=name)
    session.add(patient)
    session.commit()
    return patient


def _agent_caller(session):
    return _credential(
        session, name="b3-reception-agent", principal_type="agent", profile="conversation-agent"
    )


def _key():
    return {"Idempotency-Key": str(uuid4())}


def _ids(page):
    return [item["id"] for item in page["items"]]


def _walk(client, path, headers, *, limit, between=None):
    """Follow ``next_cursor`` to the end; ``between(i)`` runs after page ``i``."""
    seen, cursor, i = [], None, 0
    while True:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = client.get(path, params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(_ids(body))
        cursor = body["next_cursor"]
        if between is not None:
            between(i)
        i += 1
        if cursor is None:
            return seen


# --- 1. tenant isolation ----------------------------------------------------------


def test_org_a_never_sees_org_b_conversations_messages_or_handoffs(client, session):
    _pid, lucia = _lucia(session)
    mine = _conversation(session)
    _message(session, mine)
    _handoff(session, mine)
    other_org = _other_org(session, "Clinica B3")
    theirs = _conversation(session, organization_id=other_org)
    _message(session, theirs, text="Mensaje de otra clínica")
    _handoff(session, theirs)

    conversations = client.get("/conversations", headers=lucia)
    assert conversations.status_code == 200, conversations.text
    assert _ids(conversations.json()) == [mine.id]

    handoffs = client.get("/handoffs", headers=lucia)
    assert handoffs.status_code == 200, handoffs.text
    assert [h["conversation_id"] for h in handoffs.json()["items"]] == [mine.id]

    foreign = client.get(f"/conversations/{theirs.id}/messages", headers=lucia)
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    assert "otra clínica" not in foreign.text


# --- 2. keyset stability ----------------------------------------------------------


def test_conversation_cursor_never_duplicates_or_skips_under_inserts(client, session):
    _pid, lucia = _lucia(session)
    originals = [
        _conversation(session, last_message_at=BASE - timedelta(minutes=i)) for i in range(7)
    ]
    # Two rows share one timestamp: the ``id`` tie-break must hold.
    originals.append(_conversation(session, last_message_at=BASE - timedelta(minutes=3)))
    expected_order = sorted(originals, key=lambda c: (c.last_message_at, c.id), reverse=True)

    def between(i):
        # A brand-new conversation and a bump of an already-seen one: both move
        # above the cursor, so neither duplicates nor shifts the unseen rows.
        _conversation(session, last_message_at=datetime.now(UTC) + timedelta(minutes=i))
        if i == 0:
            seen_first = expected_order[0]
            row = session.get(Conversation, seen_first.id)
            row.last_message_at = datetime.now(UTC) + timedelta(hours=1)
            session.commit()

    seen = _walk(client, "/conversations", lucia, limit=3, between=between)
    original_ids = [c.id for c in expected_order]
    assert len(seen) == len(set(seen))
    assert [i for i in seen if i in original_ids] == original_ids


def test_messages_page_oldest_to_newest_without_dupes_under_appends(client, session):
    _pid, lucia = _lucia(session)
    conversation = _conversation(session)
    originals = [
        _message(session, conversation, text=f"m{i}", occurred_at=BASE + timedelta(minutes=i))
        for i in range(5)
    ]
    originals.append(_message(session, conversation, text="tie", occurred_at=BASE))

    def between(i):
        _message(session, conversation, text=f"new{i}", occurred_at=BASE + timedelta(hours=1, minutes=i))

    seen = _walk(
        client, f"/conversations/{conversation.id}/messages", lucia, limit=2, between=between
    )
    expected = [m.id for m in sorted(originals, key=lambda m: (m.occurred_at, m.id))]
    assert len(seen) == len(set(seen))
    assert seen[: len(expected)] == expected
    # Appended messages arrive after the originals, in order.
    rows = {m.id: m for m in session.scalars(select(Message)).all()}
    session.rollback()
    stamps = [(rows[i].occurred_at, i) for i in seen]
    assert stamps == sorted(stamps)


# --- 3. display name, preview, retention ------------------------------------------


def test_display_name_preview_and_retention(client, session):
    _pid, lucia = _lucia(session)
    patient = _patient(session, "María Quispe")
    lead = _lead(session, "Jorge Lead")
    with_patient = _conversation(
        session, patient_id=patient.id, lead_id=lead.id, last_message_at=BASE
    )
    with_lead = _conversation(
        session, lead_id=lead.id, last_message_at=BASE - timedelta(minutes=1)
    )
    anonymous = _conversation(
        session, phone="+12025550123", last_message_at=BASE - timedelta(minutes=2)
    )
    long_text = "x" * 200
    _message(session, with_patient, text=long_text, occurred_at=BASE - timedelta(minutes=5))
    _message(session, with_lead, text="secreto", redacted=True)
    _message(
        session,
        anonymous,
        text="caducado",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        message_type="image",
        media={"url": "https://media.example/secret.jpg"},
    )

    response = client.get("/conversations", headers=lucia)
    assert response.status_code == 200, response.text
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[with_patient.id]["contact_display_name"] == "María Quispe"
    assert by_id[with_lead.id]["contact_display_name"] == "Jorge Lead"
    assert by_id[anonymous.id]["contact_display_name"] == "+" + "•" * 8 + "123"
    preview = by_id[with_patient.id]["last_message_preview"]
    assert preview["direction"] == "inbound" and preview["text"] == "x" * 80
    assert by_id[with_lead.id]["last_message_preview"]["text"] is None
    assert by_id[anonymous.id]["last_message_preview"]["text"] is None
    assert "secreto" not in response.text and "caducado" not in response.text

    lead_msgs = client.get(f"/conversations/{with_lead.id}/messages", headers=lucia).json()
    assert lead_msgs["items"][0]["text"] is None
    assert lead_msgs["items"][0]["content_state"] == "redacted"
    anon = client.get(f"/conversations/{anonymous.id}/messages", headers=lucia)
    item = anon.json()["items"][0]
    assert item["text"] is None and item["content_state"] == "expired"
    assert item["has_media"] is True
    assert "media_reference" not in anon.text and "secret.jpg" not in anon.text
    ok = client.get(f"/conversations/{with_patient.id}/messages", headers=lucia).json()
    assert ok["items"][0]["text"] == long_text
    assert ok["items"][0]["content_state"] == "available"


# --- 4. filters & bad input -----------------------------------------------------


def test_status_and_location_filters_and_bad_cursor(client, session):
    _pid, lucia = _lucia(session)
    from test_domain_gaps_helpers import book, seed_booking

    ids = seed_booking(session, suffix="b3-loc")
    other_location = Location(
        organization_id=ORG, name="Sede Norte b3", timezone="America/Lima", is_active=True
    )
    session.add(other_location)
    session.commit()
    book(session, ids)
    at_location = _conversation(session, lead_id=ids["lead_id"])
    elsewhere = _conversation(session, last_message_at=BASE - timedelta(minutes=1))
    _handoff(session, elsewhere)

    by_status = client.get("/conversations", params={"status": "human_handoff"}, headers=lucia)
    assert by_status.status_code == 200, by_status.text
    assert _ids(by_status.json()) == [elsewhere.id]
    assert by_status.json()["items"][0]["pending_handoff_id"] is not None

    by_location = client.get(
        "/conversations", params={"location_id": ids["location_id"]}, headers=lucia
    )
    assert _ids(by_location.json()) == [at_location.id]
    empty = client.get("/conversations", params={"location_id": other_location.id}, headers=lucia)
    assert _ids(empty.json()) == []
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1
    session.rollback()

    for params in (
        {"cursor": "not-a-cursor"},
        {"status": "nope"},
        {"limit": 0},
        {"limit": 101},
        {"unknown": "1"},
    ):
        bad = client.get("/conversations", params=params, headers=lucia)
        assert bad.status_code == 422, (params, bad.text)
        assert bad.json()["error"]["code"] == "INVALID_INPUT"
    bad_messages = client.get(
        f"/conversations/{at_location.id}/messages", params={"cursor": "%%%"}, headers=lucia
    )
    assert bad_messages.status_code == 422


# --- 5. handoffs & claim --------------------------------------------------------


def _audit_count(session, action):
    value = session.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action)
    )
    session.rollback()
    return value


def test_handoff_queue_claim_replay_conflict_and_resume(client, session):
    lucia_id, lucia = _lucia(session)
    _carlos_id, carlos = _carlos(session)
    patient = _patient(session, "Rosa Huamán")
    first = _conversation(session, patient_id=patient.id)
    second = _conversation(session)
    h1 = _handoff(session, first, created_at=BASE)
    h2 = _handoff(session, second, created_at=BASE + timedelta(minutes=1))
    resolved_conv = _conversation(session)
    _handoff(session, resolved_conv, status="resolved", created_at=BASE)

    queue = client.get("/handoffs", headers=lucia)
    assert queue.status_code == 200, queue.text
    assert _ids(queue.json()) == [h1.id, h2.id]  # oldest first
    first_item = queue.json()["items"][0]
    assert first_item["contact_display_name"] == "Rosa Huamán"
    assert first_item["claimed_by_principal_id"] is None
    assert len(_ids(client.get("/handoffs", params={"status": "resolved"}, headers=lucia).json())) == 1

    key = _key()
    claimed = client.post(f"/handoffs/{h1.id}/claim", headers={**lucia, **key})
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["status"] == "claimed"
    assert body["claimed_by_principal_id"] == lucia_id
    assert body["claimed_by_display_name"] == "Lucía Ramos"
    assert _audit_count(session, "reception_handoff.claimed") == 1
    conv = session.get(Conversation, first.id)
    assert conv.assigned_principal_id == lucia_id
    session.rollback()

    replay = client.post(f"/handoffs/{h1.id}/claim", headers={**lucia, **key})
    assert replay.status_code == 200
    assert replay.json() == body
    assert replay.headers.get("Idempotent-Replay") == "true"

    again = client.post(f"/handoffs/{h1.id}/claim", headers={**lucia, **_key()})
    assert again.status_code == 200 and again.json() == body
    assert _audit_count(session, "reception_handoff.claimed") == 1

    stolen = client.post(f"/handoffs/{h1.id}/claim", headers={**carlos, **_key()})
    assert stolen.status_code == 409, stolen.text
    assert stolen.json()["error"]["code"] == "HANDOFF_NOT_PENDING"
    assert stolen.json()["error"]["details"] == {"status": "claimed"}

    claimed_list = client.get("/handoffs", params={"status": "claimed"}, headers=lucia).json()
    assert claimed_list["items"][0]["claimed_by_display_name"] == "Lucía Ramos"

    resumed = client.post(
        f"/internal/conversations/{first.id}/resume", json={}, headers={**lucia, **_key()}
    )
    assert resumed.status_code == 200, resumed.text
    resolved = client.get("/handoffs", params={"status": "resolved"}, headers=lucia).json()
    item = next(i for i in resolved["items"] if i["id"] == h1.id)
    assert item["status"] == "resolved"
    assert item["claimed_by_principal_id"] is None and item["claimed_by_display_name"] is None
    late = client.post(f"/handoffs/{h1.id}/claim", headers={**lucia, **_key()})
    assert late.status_code == 409
    assert late.json()["error"]["details"] == {"status": "resolved"}


def test_claim_requires_a_human_with_resume_and_is_tenant_scoped(client, session):
    _lid, lucia = _lucia(session)
    conversation = _conversation(session)
    handoff = _handoff(session, conversation)
    _aid, agent = _credential(
        session, name="b3-operator-agent", principal_type="agent", profile="reception-operator"
    )
    _iid, integration = _credential(
        session, name="b3-operator-int", principal_type="integration", profile="reception-operator"
    )
    _hid, read_only = _human_with(session, ("conversations.read",), name="Solo Lectura")
    for headers in (agent, integration, read_only):
        denied = client.post(f"/handoffs/{handoff.id}/claim", headers={**headers, **_key()})
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "PERMISSION_DENIED"

    other_org = _other_org(session, "Clinica B3 claim")
    foreign = _handoff(session, _conversation(session, organization_id=other_org))
    receipts_before = session.scalar(select(func.count()).select_from(CommandReceipt))
    session.rollback()
    missing = client.post(f"/handoffs/{foreign.id}/claim", headers={**lucia, **_key()})
    assert missing.status_code == 404, missing.text
    assert _audit_count(session, "reception_handoff.claimed") == 0
    assert session.scalar(select(func.count()).select_from(CommandReceipt)) == receipts_before
    row = session.get(ReceptionHandoff, foreign.id)
    assert row.status == "pending"
    session.rollback()
    no_key = client.post(f"/handoffs/{handoff.id}/claim", headers=lucia)
    assert no_key.status_code == 422


# --- 5b. agents never read staff surfaces -----------------------------------------


def test_agent_and_integration_principals_get_403_on_every_staff_read(client, session):
    conversation = _conversation(session)
    _message(session, conversation)
    callers = [
        _agent_caller(session)[1],
        _credential(
            session, name="b3-sales", principal_type="agent", profile="sales-agent-v0"
        )[1],
        _credential(
            session, name="b3-cobranza", principal_type="agent", profile="collections-agent"
        )[1],
        _credential(
            session, name="b3-n8n", principal_type="integration", profile="conversation-agent"
        )[1],
    ]
    for headers in callers:
        for path in (
            "/conversations",
            f"/conversations/{conversation.id}/messages",
            "/handoffs",
            "/activity",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 403, (path, response.text)
            assert response.json()["error"]["code"] == "PERMISSION_DENIED"
            assert "Hola" not in response.text
