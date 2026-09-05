"""Phase 2: durable conversations, provider deduplication and outbound queue."""

from __future__ import annotations

import threading
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.context import default_context
from app.db import get_db
from app.organization.service import create_organization

UTC = timezone.utc
ORG = 1


def _app_for(migrated_engine):
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
    return app


def _seed_channel(
    session,
    *,
    organization_id=ORG,
    external_id="wa-clinic-1",
    provider="whatsapp",
) -> int:
    channel_id = session.execute(
        text(
            "INSERT INTO channel_accounts "
            "(organization_id, provider, external_account_id, phone_number_id, display_name) "
            "VALUES (:org, :provider, :external, :phone, 'Canal principal') "
            "RETURNING id"
        ),
        {
            "org": organization_id,
            "provider": provider,
            "external": external_id,
            "phone": f"phone-{organization_id}-{external_id}",
        },
    ).scalar_one()
    session.commit()
    return channel_id


def _inbound(
    provider_message_id="wamid.001",
    *,
    external_account_id="wa-clinic-1",
    external_contact_id="wa-contact-51999000111",
    phone_e164="+51999000111",
    occurred_at="2026-08-20T14:00:00Z",
    provider="whatsapp",
):
    return {
        "schema_version": "1.0",
        "provider": provider,
        "channel_account_external_id": external_account_id,
        "provider_message_id": provider_message_id,
        "external_contact_id": external_contact_id,
        "phone_e164": phone_e164,
        "message_type": "text",
        "text": "Quiero reservar una cita",
        "media": None,
        "occurred_at": occurred_at,
    }


def _idempotency_headers(key=None):
    return {**AUTH_HEADERS, "Idempotency-Key": key or str(uuid4())}


def test_inbound_event_is_persisted_once_and_audited(migrated_engine, session):
    _seed_channel(session)
    app = _app_for(migrated_engine)
    correlation_id = str(uuid4())
    headers = {
        **_idempotency_headers(),
        "X-Request-Id": str(uuid4()),
        "X-Correlation-Id": correlation_id,
    }

    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post("/internal/messages/inbound", json=_inbound(), headers=headers)
        duplicate = client.post(
            "/internal/messages/inbound",
            json=_inbound(),
            headers={**headers, "X-Request-Id": str(uuid4())},
        )

    assert first.status_code == 201, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert first.json()["message_id"] == duplicate.json()["message_id"]
    assert first.json()["conversation_id"] == duplicate.json()["conversation_id"]
    assert first.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True

    assert session.execute(text("SELECT count(*) FROM contact_identities")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM messages")).scalar_one() == 1
    audit = session.execute(
        text(
            "SELECT action, correlation_id FROM audit_events "
            "WHERE action='message.received'"
        )
    ).one()
    assert audit == ("message.received", correlation_id)


def test_out_of_order_message_does_not_move_last_message_backwards(
    migrated_engine, session
):
    _seed_channel(session)
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        later = client.post(
            "/internal/messages/inbound",
            json=_inbound("wamid.later", occurred_at="2026-08-20T15:00:00Z"),
            headers=_idempotency_headers(),
        )
        earlier = client.post(
            "/internal/messages/inbound",
            json=_inbound("wamid.earlier", occurred_at="2026-08-20T13:00:00Z"),
            headers=_idempotency_headers(),
        )

    assert later.status_code == 201, later.text
    assert earlier.status_code == 201, earlier.text
    last_message_at = session.execute(
        text("SELECT last_message_at FROM conversations")
    ).scalar_one()
    assert last_message_at == datetime(2026, 8, 20, 15, 0, tzinfo=UTC)


def test_outbound_message_and_queue_row_are_atomic_and_idempotent(
    migrated_engine, session
):
    _seed_channel(session)
    key = str(uuid4())
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        inbound = client.post(
            "/internal/messages/inbound",
            json=_inbound(),
            headers=_idempotency_headers(),
        )
        conversation_id = inbound.json()["conversation_id"]
        first = client.post(
            f"/internal/conversations/{conversation_id}/outbound",
            json={"text": "Tenemos disponibilidad mañana."},
            headers=_idempotency_headers(key),
        )
        duplicate = client.post(
            f"/internal/conversations/{conversation_id}/outbound",
            json={"text": "Tenemos disponibilidad mañana."},
            headers=_idempotency_headers(key),
        )
        conflict = client.post(
            f"/internal/conversations/{conversation_id}/outbound",
            json={"text": "Un texto distinto y sensible"},
            headers=_idempotency_headers(key),
        )

    assert first.status_code == 201, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["outbound_id"] == first.json()["outbound_id"]
    assert duplicate.json()["duplicate"] is True
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert "Un texto distinto" not in conflict.text

    logical = session.execute(
        text("SELECT count(*) FROM messages WHERE direction='outbound'")
    ).scalar_one()
    queued = session.execute(text("SELECT count(*) FROM outbound_messages")).scalar_one()
    assert (logical, queued) == (1, 1)


def test_synthetic_provider_is_persisted_but_never_claimed_for_external_dispatch(
    migrated_engine, session
):
    _seed_channel(session, external_id="test-lab", provider="test")
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        inbound = client.post(
            "/internal/messages/inbound",
            json=_inbound(
                "test-message-001",
                external_account_id="test-lab",
                external_contact_id="synthetic-contact-001",
                phone_e164="+51900000001",
                provider="test",
            ),
            headers=_idempotency_headers(),
        )
        assert inbound.status_code == 201, inbound.text
        queued = client.post(
            f"/internal/conversations/{inbound.json()['conversation_id']}/outbound",
            json={"text": "Respuesta visible solo en el laboratorio."},
            headers=_idempotency_headers(),
        )
        assert queued.status_code == 201, queued.text
        claimed = client.post(
            "/internal/outbound/claim",
            json={"limit": 10},
            headers=_idempotency_headers(),
        )

    assert claimed.status_code == 200, claimed.text
    assert claimed.json() == []
    stored = session.execute(
        text(
            "SELECT c.provider, o.status, m.delivery_status "
            "FROM outbound_messages o "
            "JOIN messages m ON m.id=o.message_id "
            "JOIN conversations v ON v.id=o.conversation_id "
            "JOIN channel_accounts c ON c.id=v.channel_account_id "
            "WHERE o.id=:id"
        ),
        {"id": queued.json()["outbound_id"]},
    ).one()
    assert stored == ("test", "pending", "pending")


def test_transient_outbound_failures_reach_dead_letter_after_three_attempts(
    migrated_engine, session
):
    _seed_channel(session)
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        inbound = client.post(
            "/internal/messages/inbound",
            json=_inbound(),
            headers=_idempotency_headers(),
        )
        queued = client.post(
            f"/internal/conversations/{inbound.json()['conversation_id']}/outbound",
            json={"text": "Mensaje durable"},
            headers=_idempotency_headers(),
        )
        outbound_id = queued.json()["outbound_id"]

        for expected_attempt in (1, 2, 3):
            claimed = client.post(
                "/internal/outbound/claim",
                json={"limit": 10},
                headers=_idempotency_headers(),
            )
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()[0]["attempt_count"] == expected_attempt
            failed = client.post(
                f"/internal/outbound/{outbound_id}/result",
                json={
                    "outcome": "transient_failure",
                    "provider_message_id": None,
                    "error_code": "PROVIDER_TIMEOUT",
                },
                headers=_idempotency_headers(),
            )
            assert failed.status_code == 200, failed.text
            if expected_attempt < 3:
                assert failed.json()["status"] == "failed"
                session.execute(
                    text(
                        "UPDATE outbound_messages SET next_attempt_at=:due "
                        "WHERE id=:id"
                    ),
                    {
                        "due": datetime.now(UTC) - timedelta(seconds=1),
                        "id": outbound_id,
                    },
                )
                session.commit()

    assert failed.json()["status"] == "dead_letter"
    stored = session.execute(
        text(
            "SELECT status, attempt_count, last_error_code "
            "FROM outbound_messages WHERE id=:id"
        ),
        {"id": outbound_id},
    ).one()
    assert stored == ("dead_letter", 3, "PROVIDER_TIMEOUT")


def test_expired_third_processing_lease_is_dead_lettered(migrated_engine, session):
    _seed_channel(session)
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        inbound = client.post(
            "/internal/messages/inbound",
            json=_inbound(),
            headers=_idempotency_headers(),
        )
        queued = client.post(
            f"/internal/conversations/{inbound.json()['conversation_id']}/outbound",
            json={"text": "No debe quedar bloqueado"},
            headers=_idempotency_headers(),
        )
        outbound_id = queued.json()["outbound_id"]

        for expected_attempt in (1, 2, 3):
            claimed = client.post(
                "/internal/outbound/claim",
                json={"limit": 10},
                headers=_idempotency_headers(),
            )
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()[0]["attempt_count"] == expected_attempt
            session.execute(
                text(
                    "UPDATE outbound_messages SET next_attempt_at=:due "
                    "WHERE id=:id"
                ),
                {
                    "due": datetime.now(UTC) - timedelta(seconds=1),
                    "id": outbound_id,
                },
            )
            session.commit()

        final_claim = client.post(
            "/internal/outbound/claim",
            json={"limit": 10},
            headers=_idempotency_headers(),
        )

    assert final_claim.status_code == 200
    assert final_claim.json() == []
    stored = session.execute(
        text(
            "SELECT o.status, o.attempt_count, o.last_error_code, m.delivery_status "
            "FROM outbound_messages o JOIN messages m ON m.id=o.message_id "
            "WHERE o.id=:id"
        ),
        {"id": outbound_id},
    ).one()
    assert stored == (
        "dead_letter",
        3,
        "PROCESSING_LEASE_EXPIRED",
        "dead_letter",
    )


def test_two_concurrent_messages_share_one_contact_and_conversation(
    migrated_engine, session
):
    _seed_channel(session)
    from app.messaging.schemas import InboundMessageCreate
    from app.messaging.service import ingest_inbound_message

    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def ingest(provider_message_id):
        db = maker()
        try:
            barrier.wait()
            results.append(
                ingest_inbound_message(
                    db,
                    InboundMessageCreate(**_inbound(provider_message_id)),
                    ctx=default_context(ORG),
                )
            )
        except Exception as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)
        finally:
            db.close()

    threads = [
        threading.Thread(target=ingest, args=("wamid.concurrent-a",)),
        threading.Thread(target=ingest, args=("wamid.concurrent-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(results) == 2
    assert session.execute(text("SELECT count(*) FROM contact_identities")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM messages")).scalar_one() == 2


def test_same_phone_is_isolated_between_organizations(session):
    from app.messaging.schemas import InboundMessageCreate
    from app.messaging.service import ingest_inbound_message

    org_b = create_organization(session, "Clínica B").id
    _seed_channel(session, organization_id=ORG, external_id="wa-a")
    _seed_channel(session, organization_id=org_b, external_id="wa-b")

    a = ingest_inbound_message(
        session,
        InboundMessageCreate(**_inbound("wamid.a", external_account_id="wa-a")),
        ctx=default_context(ORG),
    )
    b = ingest_inbound_message(
        session,
        InboundMessageCreate(**_inbound("wamid.b", external_account_id="wa-b")),
        ctx=default_context(org_b),
    )

    assert a.contact_identity_id != b.contact_identity_id
    tenants = session.execute(
        text(
            "SELECT organization_id, normalized_phone_e164 FROM contact_identities "
            "ORDER BY organization_id"
        )
    ).all()
    assert tenants == [(ORG, "+51999000111"), (org_b, "+51999000111")]


def test_inbound_contract_rejects_embedded_binary_or_unknown_fields(
    migrated_engine, session
):
    _seed_channel(session)
    payload = _inbound()
    payload["raw_audio_base64"] = "A" * 100
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.post(
            "/internal/messages/inbound",
            json=payload,
            headers=_idempotency_headers(),
        )

    assert response.status_code == 422
    assert session.execute(text("SELECT count(*) FROM messages")).scalar_one() == 0


@pytest.mark.parametrize(
    "key",
    ("business-derived-key", str(uuid4()).upper(), "6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
)
def test_messaging_mutations_require_canonical_uuid4_transport_keys(
    migrated_engine, session, key
):
    _seed_channel(session)
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.post(
            "/internal/messages/inbound",
            json=_inbound(),
            headers=_idempotency_headers(key),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert session.execute(text("SELECT count(*) FROM messages")).scalar_one() == 0


def test_expired_content_is_redacted_without_losing_delivery_metadata(
    migrated_engine, session
):
    from app.messaging.service import redact_expired_message_content

    _seed_channel(session)
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.post(
            "/internal/messages/inbound",
            json=_inbound("wamid.retention"),
            headers=_idempotency_headers(),
        )
    assert response.status_code == 201, response.text
    message_id = response.json()["message_id"]
    session.execute(
        text("UPDATE messages SET content_expires_at=:expired WHERE id=:id"),
        {"expired": datetime.now(UTC) - timedelta(seconds=1), "id": message_id},
    )
    session.commit()

    assert (
        redact_expired_message_content(
            session,
            organization_id=ORG,
            now=datetime.now(UTC),
        )
        == 1
    )

    stored = session.execute(
        text(
            "SELECT body_text, media_reference, content_redacted_at, "
            "provider_message_id, delivery_status FROM messages WHERE id=:id"
        ),
        {"id": message_id},
    ).one()
    assert stored.body_text is None
    assert stored.media_reference is None
    assert stored.content_redacted_at is not None
    assert stored.provider_message_id == "wamid.retention"
    assert stored.delivery_status == "received"


def test_expired_content_redaction_is_explicitly_tenant_scoped(session):
    from app.messaging.service import redact_expired_message_content

    org_b = session.execute(
        text("INSERT INTO organizations (name) VALUES ('Redact B') RETURNING id")
    ).scalar_one()
    channel_a = _seed_channel(session, organization_id=ORG, external_id="redact-a")
    channel_b = _seed_channel(session, organization_id=org_b, external_id="redact-b")
    now = datetime.now(UTC)
    expired = now - timedelta(minutes=1)

    contact_a = session.execute(
        text(
            "INSERT INTO contact_identities "
            "(organization_id, channel_account_id, external_contact_id, normalized_phone_e164) "
            "VALUES (:org, :channel, :external, :phone) RETURNING id"
        ),
        {
            "org": ORG,
            "channel": channel_a,
            "external": "redact-contact-a",
            "phone": "+51999000121",
        },
    ).scalar_one()
    contact_b = session.execute(
        text(
            "INSERT INTO contact_identities "
            "(organization_id, channel_account_id, external_contact_id, normalized_phone_e164) "
            "VALUES (:org, :channel, :external, :phone) RETURNING id"
        ),
        {
            "org": org_b,
            "channel": channel_b,
            "external": "redact-contact-b",
            "phone": "+51999000122",
        },
    ).scalar_one()
    conversation_a = session.execute(
        text(
            "INSERT INTO conversations "
            "(organization_id, channel_account_id, contact_identity_id, last_message_at) "
            "VALUES (:org, :channel, :contact, :occurred) RETURNING id"
        ),
        {"org": ORG, "channel": channel_a, "contact": contact_a, "occurred": expired},
    ).scalar_one()
    conversation_b = session.execute(
        text(
            "INSERT INTO conversations "
            "(organization_id, channel_account_id, contact_identity_id, last_message_at) "
            "VALUES (:org, :channel, :contact, :occurred) RETURNING id"
        ),
        {"org": org_b, "channel": channel_b, "contact": contact_b, "occurred": expired},
    ).scalar_one()
    message_a = session.execute(
        text(
            "INSERT INTO messages "
            "(organization_id, channel_account_id, conversation_id, direction, "
            "provider_message_id, message_type, body_text, delivery_status, "
            "occurred_at, content_expires_at) "
            "VALUES (:org, :channel, :conversation, 'inbound', :provider, 'text', "
            ":body, 'received', :occurred, :expires) RETURNING id"
        ),
        {
            "org": ORG,
            "channel": channel_a,
            "conversation": conversation_a,
            "provider": "redact-message-a",
            "body": "tenant A secret",
            "occurred": expired,
            "expires": expired,
        },
    ).scalar_one()
    message_b = session.execute(
        text(
            "INSERT INTO messages "
            "(organization_id, channel_account_id, conversation_id, direction, "
            "provider_message_id, message_type, body_text, delivery_status, "
            "occurred_at, content_expires_at) "
            "VALUES (:org, :channel, :conversation, 'inbound', :provider, 'text', "
            ":body, 'received', :occurred, :expires) RETURNING id"
        ),
        {
            "org": org_b,
            "channel": channel_b,
            "conversation": conversation_b,
            "provider": "redact-message-b",
            "body": "tenant B secret",
            "occurred": expired,
            "expires": expired,
        },
    ).scalar_one()
    session.commit()

    assert (
        redact_expired_message_content(
            session,
            organization_id=ORG,
            now=now,
        )
        == 1
    )
    assert session.execute(text("SELECT body_text FROM messages WHERE id=:id"), {"id": message_a}).scalar_one() is None
    assert session.execute(text("SELECT body_text FROM messages WHERE id=:id"), {"id": message_b}).scalar_one() == "tenant B secret"


def test_redaction_script_requires_an_explicit_organization_id():
    result = subprocess.run(
        [sys.executable, "scripts/redact_message_content.py", "--limit", "1"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--organization-id" in result.stderr
