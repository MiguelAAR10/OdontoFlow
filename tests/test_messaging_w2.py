"""W2: authenticated conversation listing and deterministic close."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import AUTH_HEADERS
from test_authentication import auth, make_integration
from test_messaging_phase2 import _app_for, _inbound, _seed_channel

UTC = timezone.utc
ORG = 1


def _seed_conversation(
    session,
    *,
    organization_id: int = ORG,
    status: str = "open",
    last_message_at: str = "2026-08-20T14:00:00Z",
    suffix: str = "conversation",
) -> int:
    channel_id = _seed_channel(
        session,
        organization_id=organization_id,
        external_id=f"w2-channel-{organization_id}-{suffix}",
        provider="test",
    )
    contact_id = session.execute(
        text(
            "INSERT INTO contact_identities "
            "(organization_id, channel_account_id, external_contact_id, "
            "normalized_phone_e164) "
            "VALUES (:org, :channel, :external, :phone) RETURNING id"
        ),
        {
            "org": organization_id,
            "channel": channel_id,
            "external": f"w2-contact-{organization_id}-{suffix}",
            "phone": f"+5199900{organization_id:02d}{len(suffix):03d}",
        },
    ).scalar_one()
    conversation_id = session.execute(
        text(
            "INSERT INTO conversations "
            "(organization_id, channel_account_id, contact_identity_id, status, "
            "last_message_at) "
            "VALUES (:org, :channel, :contact, :status, :last_message_at) "
            "RETURNING id"
        ),
        {
            "org": organization_id,
            "channel": channel_id,
            "contact": contact_id,
            "status": status,
            "last_message_at": last_message_at,
        },
    ).scalar_one()
    session.commit()
    return conversation_id


def _key() -> str:
    return str(uuid4())


def test_conversation_listing_requires_authentication(migrated_engine, session):
    _seed_conversation(session, suffix="auth")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.get("/internal/conversations")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_authenticated_listing_is_tenant_scoped_and_typed(migrated_engine, session):
    _seed_conversation(session, suffix="tenant-a")
    organization_b = session.execute(
        text("INSERT INTO organizations (name) VALUES ('W2 tenant B') RETURNING id")
    ).scalar_one()
    _seed_conversation(session, organization_id=organization_b, suffix="tenant-b")
    session.commit()
    _principal, _credential, token_b = make_integration(
        session, organization_id=organization_b, name="w2-list-b"
    )

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        rows_a = client.get("/internal/conversations", headers=AUTH_HEADERS)
        rows_b = client.get("/internal/conversations", headers=auth(token_b))

    assert rows_a.status_code == 200, rows_a.text
    assert rows_b.status_code == 200, rows_b.text
    assert len(rows_a.json()) == 1
    assert len(rows_b.json()) == 1
    assert set(rows_a.json()[0]) == {
        "conversation_id",
        "contact_identity_id",
        "status",
        "last_message_at",
    }
    assert rows_a.json()[0]["status"] == "open"
    assert rows_a.json()[0]["conversation_id"] != rows_b.json()[0]["conversation_id"]


def test_listing_status_filter_and_bounded_limit(migrated_engine, session):
    _seed_conversation(session, status="open", suffix="open-1")
    _seed_conversation(session, status="open", suffix="open-2")
    _seed_conversation(session, status="closed", suffix="closed")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        limited = client.get(
            "/internal/conversations?status=open&limit=1", headers=AUTH_HEADERS
        )
        closed = client.get(
            "/internal/conversations?status=closed&limit=10", headers=AUTH_HEADERS
        )
        too_many = client.get(
            "/internal/conversations?limit=101", headers=AUTH_HEADERS
        )

    assert limited.status_code == 200, limited.text
    assert len(limited.json()) == 1
    assert limited.json()[0]["status"] == "open"
    assert closed.status_code == 200, closed.text
    assert len(closed.json()) == 1
    assert closed.json()[0]["status"] == "closed"
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "INVALID_INPUT"


def test_listing_last_message_before_is_exclusive(migrated_engine, session):
    _seed_conversation(
        session, last_message_at="2026-08-20T13:59:59Z", suffix="before"
    )
    _seed_conversation(
        session, last_message_at="2026-08-20T14:00:00Z", suffix="exact"
    )
    _seed_conversation(session, last_message_at="2026-08-20T14:00:01Z", suffix="after")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.get(
            "/internal/conversations?last_message_before=2026-08-20T14:00:00Z",
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    assert response.json()[0]["last_message_at"] == "2026-08-20T13:59:59Z"


def test_close_is_authenticated_audited_and_deterministic(migrated_engine, session):
    conversation_id = _seed_conversation(session, suffix="close")
    key = _key()

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        first = client.post(
            f"/internal/conversations/{conversation_id}/close",
            headers={**AUTH_HEADERS, "Idempotency-Key": key},
        )
        replay = client.post(
            f"/internal/conversations/{conversation_id}/close",
            headers={**AUTH_HEADERS, "Idempotency-Key": key},
        )
        repeat = client.post(
            f"/internal/conversations/{conversation_id}/close",
            headers={**AUTH_HEADERS, "Idempotency-Key": _key()},
        )

    assert first.status_code == 200, first.text
    assert first.json() == {
        "conversation_id": conversation_id,
        "status": "closed",
        "replayed": False,
    }
    assert replay.status_code == 200, replay.text
    assert replay.json() == {
        "conversation_id": conversation_id,
        "status": "closed",
        "replayed": True,
    }
    assert repeat.status_code == 409, repeat.text
    assert repeat.json()["error"]["code"] == "ENTITY_INACTIVE"
    assert session.execute(
        text("SELECT status FROM conversations WHERE id=:id"), {"id": conversation_id}
    ).scalar_one() == "closed"
    assert session.execute(
        text(
            "SELECT count(*) FROM audit_events "
            "WHERE entity_type='conversation' AND entity_id=:id "
            "AND action='conversation.closed'"
        ),
        {"id": str(conversation_id)},
    ).scalar_one() == 1


def test_close_releases_one_open_contact_for_reopen(migrated_engine, session):
    _seed_channel(session, external_id="w2-reopen", provider="test")
    app = _app_for(migrated_engine)
    with TestClient(app, raise_server_exceptions=False) as client:
        inbound = client.post(
            "/internal/messages/inbound",
            json=_inbound(
                "w2-reopen-1",
                external_account_id="w2-reopen",
                external_contact_id="w2-reopen-contact",
                phone_e164="+51999000999",
                provider="test",
            ),
            headers={**AUTH_HEADERS, "Idempotency-Key": _key()},
        )
        assert inbound.status_code == 201, inbound.text
        old_id = inbound.json()["conversation_id"]
        closed = client.post(
            f"/internal/conversations/{old_id}/close",
            headers={**AUTH_HEADERS, "Idempotency-Key": _key()},
        )
        reopened = client.post(
            "/internal/messages/inbound",
            json=_inbound(
                "w2-reopen-2",
                external_account_id="w2-reopen",
                external_contact_id="w2-reopen-contact",
                phone_e164="+51999000999",
                provider="test",
                occurred_at="2026-08-20T15:00:00Z",
            ),
            headers={**AUTH_HEADERS, "Idempotency-Key": _key()},
        )

    assert closed.status_code == 200, closed.text
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["conversation_id"] != old_id
    assert session.execute(
        text(
            "SELECT count(*) FROM conversations "
            "WHERE organization_id=:org AND contact_identity_id="
            "(SELECT contact_identity_id FROM conversations WHERE id=:id) "
            "AND status <> 'closed'"
        ),
        {"org": ORG, "id": old_id},
    ).scalar_one() == 1


def test_close_cross_tenant_conversation_is_not_found(migrated_engine, session):
    conversation_id = _seed_conversation(session, suffix="tenant-a-close")
    organization_b = session.execute(
        text("INSERT INTO organizations (name) VALUES ('W2 close tenant B') RETURNING id")
    ).scalar_one()
    session.commit()
    _principal, _credential, token_b = make_integration(
        session, organization_id=organization_b, name="w2-close-b"
    )

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.post(
            f"/internal/conversations/{conversation_id}/close",
            headers={**auth(token_b), "Idempotency-Key": _key()},
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"
