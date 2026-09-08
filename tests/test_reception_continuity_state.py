"""Advisory reception checkpoints share outbound atomicity and retention."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.messaging.models import Message, OutboundMessage
from app.messaging.service import redact_expired_message_content
from app.context import default_context
from app.organization.service import create_organization
from test_messaging_phase2 import (
    _app_for,
    _idempotency_headers,
    _inbound,
    _seed_channel,
)


@pytest.fixture
def client(migrated_engine, session):
    _seed_channel(session)
    return TestClient(_app_for(migrated_engine), raise_server_exceptions=False)


def _new_inbound(client, *, suffix="one"):
    response = client.post(
        "/internal/messages/inbound",
        json=_inbound(
            f"wamid.continuity-{suffix}",
            external_contact_id=f"continuity-{suffix}",
            occurred_at=datetime.now(UTC).isoformat(),
        ),
        headers=_idempotency_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _state(message_id, **changes):
    value = {
        "schema_version": "reception-state-v1",
        "intent": "booking",
        "phase": "awaiting_name",
        "service_id": 1,
        "location_id": 1,
        "window_start": "2026-09-07T09:00:00-05:00",
        "window_end": "2026-09-07T13:00:00-05:00",
        "offered_slots": [{"practitioner_id": 1, "start": "2026-09-07T09:00:00-05:00"}],
        "selected_slot": {"practitioner_id": 1, "start": "2026-09-07T09:00:00-05:00"},
        "appointment_id": None,
        "updated_at": "2026-09-05T10:00:00-05:00",
        "last_message_id": message_id,
    }
    return {**value, **changes}


def _enqueue(client, inbound, state, *, key=None):
    return client.post(
        f"/internal/conversations/{inbound['conversation_id']}/outbound",
        json={"text": "Claro, ¿a nombre de quién reservo?", "reception_state": state},
        headers=_idempotency_headers(key),
    )


def _context(client, conversation_id):
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    response = client.post(
        "/agent-tools/call",
        json={
            "tool_version": "1.0",
            "tool_name": "get_reception_context",
            "conversation_id": conversation_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "idempotency_key": None,
            "arguments": {"as_of": datetime.now(UTC).date().isoformat()},
        },
        headers={
            **_idempotency_headers(),
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success", response.text
    return response.json()["data"]


def test_checkpoint_is_atomic_idempotent_and_internal_only(client, session):
    inbound = _new_inbound(client)
    state = _state(inbound["message_id"])
    key = str(uuid4())
    first = _enqueue(client, inbound, state, key=key)
    assert first.status_code == 201, first.text
    replay = _enqueue(client, inbound, state, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["duplicate"] is True
    assert replay.json()["outbound_id"] == first.json()["outbound_id"]
    conflict = _enqueue(client, inbound, {**state, "phase": "completed"}, key=key)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    rows = list(session.scalars(select(OutboundMessage)))
    assert len(rows) == 1
    assert rows[0].payload["_reception_state"] == state
    assert session.get(Message, rows[0].message_id).body_text == "Claro, ¿a nombre de quién reservo?"
    session.rollback()
    context = _context(client, inbound["conversation_id"])
    assert context["reception_state"] == state
    assert context["pending_action"] is None
    claimed = client.post(
        "/internal/outbound/claim", json={"limit": 10}, headers=_idempotency_headers()
    )
    assert claimed.status_code == 200, claimed.text
    assert len(claimed.json()) == 1
    assert "_reception_state" not in claimed.json()[0]["payload"]
    assert all(not key.startswith("_") for key in claimed.json()[0]["payload"])
    assert "reception_state" not in first.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"patient_name": "Must not be retained"},
        {"confirmation_token": str(uuid4())},
        {"phase": "booked"},
        {"intent": "diagnosis"},
        {"service_id": -1},
        {"last_message_id": 0},
        {"last_message_id": True},
        {"updated_at": "2026-09-05T10:00:00"},
        {"updated_at": 1788620400},
        {"updated_at": "1788620400"},
        {"window_start": "2026-09-07T09:00:00"},
        {"window_end": "2026-09-07T08:00:00-05:00"},
        {"offered_slots": [{"practitioner_id": 1, "start": "2026-09-07T09:00:00-05:00"}] * 4},
        {"selected_slot": {"practitioner_id": 1, "start": "2026-09-07T09:00:00"}},
        {"selected_slot": {"practitioner_id": 1, "start": 1788620400}},
        {"selected_slot": {"practitioner_id": 1, "start": "1788620400"}},
        {"selected_slot": {"practitioner_id": 1, "start": "2026-09-07T09:00:00-05:00", "token": "no"}},
    ],
)
def test_checkpoint_contract_rejects_invalid_or_sensitive_fields(client, session, changes):
    inbound = _new_inbound(client)
    result = _enqueue(client, inbound, _state(inbound["message_id"], **changes))
    assert result.status_code == 422, result.text
    assert list(session.scalars(select(OutboundMessage))) == []


def test_checkpoint_source_must_be_a_visible_inbound_in_this_conversation(client, session):
    inbound = _new_inbound(client)
    other = _new_inbound(client, suffix="other")
    result = _enqueue(client, inbound, _state(other["message_id"]))
    assert result.status_code == 404, result.text
    assert list(session.scalars(select(OutboundMessage))) == []
    own_message = session.get(Message, inbound["message_id"])
    own_message.content_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    expired = _enqueue(client, inbound, _state(inbound["message_id"]))
    assert expired.status_code == 404, expired.text
    assert list(session.scalars(select(OutboundMessage))) == []


def test_checkpoint_cannot_reference_another_tenants_message(client, session):
    from app.messaging.schemas import InboundMessageCreate
    from app.messaging.service import ingest_inbound_message

    inbound = _new_inbound(client)
    other_org_id = create_organization(session, "Otra clínica").id
    _seed_channel(session, organization_id=other_org_id, external_id="other-clinic")
    other = ingest_inbound_message(
        session,
        InboundMessageCreate(**_inbound(
            "wamid.other-tenant",
            external_account_id="other-clinic",
            occurred_at=datetime.now(UTC).isoformat(),
        )),
        ctx=default_context(other_org_id),
    )
    rejected = _enqueue(client, inbound, _state(other.message_id))
    assert rejected.status_code == 404, rejected.text
    assert list(session.scalars(select(OutboundMessage))) == []


def test_context_uses_latest_checkpoint_without_leaking_another_contact(client, session):
    inbound = _new_inbound(client)
    other = _new_inbound(client, suffix="other")
    assert _context(client, inbound["conversation_id"])["reception_state"] is None
    first = _enqueue(client, inbound, _state(inbound["message_id"]))
    assert first.status_code == 201, first.text
    latest_state = _state(inbound["message_id"], phase="completed")
    latest = _enqueue(client, inbound, latest_state)
    assert latest.status_code == 201, latest.text
    stranger = _enqueue(client, other, _state(other["message_id"], phase="handoff"))
    assert stranger.status_code == 201, stranger.text
    assert _context(client, inbound["conversation_id"])["reception_state"] == latest_state
    legacy = client.post(
        f"/internal/conversations/{inbound['conversation_id']}/outbound",
        json={"text": "También aceptamos consultas por aquí."},
        headers=_idempotency_headers(),
    )
    assert legacy.status_code == 201, legacy.text
    assert _context(client, inbound["conversation_id"])["reception_state"] == latest_state


@pytest.mark.parametrize("visibility", ["expired", "redacted", "source_expired", "source_redacted"])
def test_context_ignores_checkpoint_when_content_is_no_longer_visible(client, session, visibility):
    inbound = _new_inbound(client)
    sent = _enqueue(client, inbound, _state(inbound["message_id"]))
    assert sent.status_code == 201, sent.text
    message_id = inbound["message_id"] if visibility.startswith("source_") else sent.json()["message_id"]
    message = session.get(Message, message_id)
    if visibility.endswith("expired"):
        message.content_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        message.content_redacted_at = datetime.now(UTC)
    session.commit()
    assert _context(client, inbound["conversation_id"])["reception_state"] is None


def test_retention_removes_checkpoint_and_context_tolerates_legacy_invalid_metadata(client, session):
    inbound = _new_inbound(client)
    sent = _enqueue(client, inbound, _state(inbound["message_id"]))
    assert sent.status_code == 201, sent.text
    outbound = session.get(OutboundMessage, sent.json()["outbound_id"])
    outbound.payload = {**outbound.payload, "_reception_state": {"phase": "unknown"}}
    session.commit()
    assert _context(client, inbound["conversation_id"])["reception_state"] is None
    session.get(Message, sent.json()["message_id"]).content_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert redact_expired_message_content(session, now=datetime.now(UTC)) == 1
    session.expire_all()
    assert "_reception_state" not in session.get(OutboundMessage, sent.json()["outbound_id"]).payload


def test_checkpoint_replay_remains_idempotent_after_retention(client, session):
    inbound = _new_inbound(client)
    state = _state(inbound["message_id"])
    key = str(uuid4())
    sent = _enqueue(client, inbound, state, key=key)
    assert sent.status_code == 201, sent.text
    session.get(Message, sent.json()["message_id"]).content_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert redact_expired_message_content(session, now=datetime.now(UTC)) == 1
    replay = _enqueue(client, inbound, state, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["outbound_id"] == sent.json()["outbound_id"]
    conflict = _enqueue(client, inbound, {**state, "phase": "completed"}, key=key)
    assert conflict.status_code == 409, conflict.text
    assert _context(client, inbound["conversation_id"])["reception_state"] is None


def test_checkpoint_and_outbound_rollback_together_when_audit_fails(client, session, monkeypatch):
    inbound = _new_inbound(client)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("app.messaging.service.record_event", fail_audit)
    response = _enqueue(client, inbound, _state(inbound["message_id"]))
    # Invalid contract initially yields 422; implemented atomic path must reach audit.
    assert response.status_code == 500, response.text
    assert list(session.scalars(select(OutboundMessage))) == []
    assert list(session.scalars(select(Message).where(Message.direction == "outbound"))) == []
