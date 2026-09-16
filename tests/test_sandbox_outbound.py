"""Real-PostgreSQL proofs for the first-class local sandbox delivery path."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from conftest import AUTH_HEADERS
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.context import default_context
from app.db import get_db
from app.iam.credentials import issue_credential
from app.messaging.schemas import InboundMessageCreate
from app.messaging.service import enqueue_outbound_message, ingest_inbound_message
from app.organization.service import create_organization

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
    organization_id: int = ORG,
    provider: str = "sandbox",
    external_id: str = "local-sandbox",
) -> int:
    channel_id = session.execute(
        text(
            "INSERT INTO channel_accounts "
            "(organization_id, provider, external_account_id, phone_number_id, display_name) "
            "VALUES (:org, :provider, :external, :phone, 'Local sandbox') "
            "RETURNING id"
        ),
        {
            "org": organization_id,
            "provider": provider,
            "external": external_id,
            "phone": f"sandbox-phone-{external_id}",
        },
    ).scalar_one()
    session.commit()
    return channel_id


def _inbound(*, provider: str = "sandbox", external_id: str = "local-sandbox") -> dict:
    return {
        "schema_version": "1.0",
        "provider": provider,
        "channel_account_external_id": external_id,
        "provider_message_id": f"sandbox-inbound-{uuid4()}",
        "external_contact_id": f"sandbox-contact-{uuid4()}",
        "phone_e164": "+51999000111",
        "message_type": "text",
        "text": "synthetic sandbox inbound",
        "occurred_at": "2026-09-16T14:00:00Z",
    }


def _idempotency_headers() -> dict[str, str]:
    return {**AUTH_HEADERS, "Idempotency-Key": str(uuid4())}


def _auth_idempotency_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid4()),
    }


def _dispatcher_token(session, *, organization_id: int = ORG) -> str:
    from scripts.issue_credential import _assign_profile, _resolve_principal

    name = f"sandbox-dispatcher-{uuid4()}"
    principal = _resolve_principal(
        session,
        organization_id=organization_id,
        name=name,
        principal_type="integration",
    )
    _assign_profile(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        profile="outbound-dispatcher",
    )
    _credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=name,
    )
    session.commit()
    return token


def _consumer_for(client, token: str, *, receiver_client=None):
    from integrations.sandbox.consumer import SandboxConsumer, SandboxSettings

    settings = SandboxSettings(
        backend_base_url="http://127.0.0.1:8000",
        receiver_url="http://127.0.0.1:8000/internal/sandbox/receive",
        credential=token,
    )
    return SandboxConsumer(
        settings,
        http_client=client,
        receiver_client=receiver_client or client,
    )


class _FailOnceReceiverClient:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def post(self, url, **kwargs):
        if str(url).endswith("/internal/sandbox/receive") and self.calls == 0:
            self.calls += 1
            request = httpx.Request("POST", str(url))
            return httpx.Response(503, request=request)
        return self.delegate.post(url, **kwargs)


def _queue_outbound(client, *, provider: str, external_id: str, text_body: str) -> int:
    inbound = client.post(
        "/internal/messages/inbound",
        json=_inbound(provider=provider, external_id=external_id),
        headers=_idempotency_headers(),
    )
    assert inbound.status_code == 201, inbound.text
    outbound = client.post(
        f"/internal/conversations/{inbound.json()['conversation_id']}/outbound",
        json={"text": text_body},
        headers=_idempotency_headers(),
    )
    assert outbound.status_code == 201, outbound.text
    return outbound.json()["outbound_id"]


def test_sandbox_provider_persists_an_outbound_without_claiming_it_yet(
    migrated_engine, session
):
    _seed_channel(session)

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        inbound = client.post(
            "/internal/messages/inbound",
            json=_inbound(),
            headers=_idempotency_headers(),
        )
        assert inbound.status_code == 201, inbound.text
        outbound = client.post(
            f"/internal/conversations/{inbound.json()['conversation_id']}/outbound",
            json={"text": "synthetic sandbox outbound"},
            headers=_idempotency_headers(),
        )

    assert outbound.status_code == 201, outbound.text
    stored = session.execute(
        text(
            "SELECT c.provider, o.status, m.delivery_status "
            "FROM outbound_messages o "
            "JOIN messages m ON m.id=o.message_id "
            "JOIN conversations v ON v.id=o.conversation_id "
            "JOIN channel_accounts c ON c.id=v.channel_account_id "
            "WHERE o.id=:id"
        ),
        {"id": outbound.json()["outbound_id"]},
    ).one()
    assert stored == ("sandbox", "pending", "pending")


def test_explicit_sandbox_claim_filter_excludes_whatsapp(
    migrated_engine, session
):
    _seed_channel(session, provider="sandbox", external_id="sandbox-only")
    _seed_channel(session, provider="whatsapp", external_id="whatsapp-only")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        sandbox_id = _queue_outbound(
            client,
            provider="sandbox",
            external_id="sandbox-only",
            text_body="sandbox delivery",
        )
        whatsapp_id = _queue_outbound(
            client,
            provider="whatsapp",
            external_id="whatsapp-only",
            text_body="live provider delivery",
        )
        claimed = client.post(
            "/internal/outbound/claim",
            json={"limit": 10, "provider": "sandbox"},
            headers=_idempotency_headers(),
        )

    assert claimed.status_code == 200, claimed.text
    assert [item["outbound_id"] for item in claimed.json()] == [sandbox_id]
    statuses = session.execute(
        text(
            "SELECT id, status FROM outbound_messages "
            "WHERE id IN (:sandbox_id, :whatsapp_id) ORDER BY id"
        ),
        {"sandbox_id": sandbox_id, "whatsapp_id": whatsapp_id},
    ).all()
    assert statuses == [(sandbox_id, "processing"), (whatsapp_id, "pending")]


def test_sandbox_receiver_binds_one_authorized_receipt_and_replays_it(
    migrated_engine, session
):
    _seed_channel(session)
    dispatcher_token = _dispatcher_token(session)

    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        outbound_id = _queue_outbound(
            client,
            provider="sandbox",
            external_id="local-sandbox",
            text_body="one local receipt",
        )
        claimed = client.post(
            "/internal/outbound/claim",
            json={"limit": 1, "provider": "sandbox"},
            headers=_auth_idempotency_headers(dispatcher_token),
        )
        assert claimed.status_code == 200, claimed.text
        item = claimed.json()[0]
        assert item["outbound_id"] == outbound_id

        first = client.post(
            "/internal/sandbox/receive",
            json={"outbound_id": outbound_id, "payload": item["payload"]},
            headers=_auth_idempotency_headers(dispatcher_token),
        )
        duplicate = client.post(
            "/internal/sandbox/receive",
            json={"outbound_id": outbound_id, "payload": item["payload"]},
            headers=_auth_idempotency_headers(dispatcher_token),
        )
        settled = client.post(
            f"/internal/outbound/{outbound_id}/result",
            json={
                "outcome": "delivered",
                "provider_message_id": f"sandbox-{outbound_id}",
            },
            headers=_auth_idempotency_headers(dispatcher_token),
        )

    assert first.status_code == 201, first.text
    assert first.json() == {
        "outbound_id": outbound_id,
        "provider_message_id": f"sandbox-{outbound_id}",
        "duplicate": False,
    }
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json() == {
        "outbound_id": outbound_id,
        "provider_message_id": f"sandbox-{outbound_id}",
        "duplicate": True,
    }
    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "delivered"
    assert session.execute(text("SELECT count(*) FROM sandbox_delivery_receipts")).scalar_one() == 1
    stored = session.execute(
        text(
            "SELECT o.status, m.delivery_status, r.provider_message_id "
            "FROM outbound_messages o "
            "JOIN messages m ON m.id=o.message_id "
            "JOIN sandbox_delivery_receipts r ON r.outbound_id=o.id "
            "WHERE o.id=:id"
        ),
        {"id": outbound_id},
    ).one()
    assert stored == ("delivered", "delivered", f"sandbox-{outbound_id}")


def test_sandbox_settlement_rejects_success_without_a_local_receipt(
    migrated_engine, session
):
    _seed_channel(session)
    dispatcher_token = _dispatcher_token(session)

    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        outbound_id = _queue_outbound(
            client,
            provider="sandbox",
            external_id="local-sandbox",
            text_body="must have a receipt",
        )
        claimed = client.post(
            "/internal/outbound/claim",
            json={"limit": 1, "provider": "sandbox"},
            headers=_auth_idempotency_headers(dispatcher_token),
        )
        assert claimed.status_code == 200, claimed.text
        settled = client.post(
            f"/internal/outbound/{outbound_id}/result",
            json={
                "outcome": "delivered",
                "provider_message_id": f"sandbox-{outbound_id}",
            },
            headers=_auth_idempotency_headers(dispatcher_token),
        )

    assert settled.status_code == 422, settled.text
    assert settled.json()["error"]["code"] == "INVALID_INPUT"
    assert session.execute(
        text("SELECT status FROM outbound_messages WHERE id=:id"),
        {"id": outbound_id},
    ).scalar_one() == "processing"
    assert session.execute(text("SELECT count(*) FROM sandbox_delivery_receipts")).scalar_one() == 0


def test_sandbox_consumer_delivers_once_and_second_poll_is_empty(migrated_engine, session):
    _seed_channel(session)
    dispatcher_token = _dispatcher_token(session)

    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        outbound_id = _queue_outbound(
            client,
            provider="sandbox",
            external_id="local-sandbox",
            text_body="consumer delivery",
        )
        consumer = _consumer_for(client, dispatcher_token)
        first = consumer.dispatch_once()
        second = consumer.dispatch_once()
        consumer.close()

    assert first.claimed == 1
    assert first.delivered == 1
    assert first.failed == 0
    assert second.claimed == 0
    assert second.delivered == 0
    assert second.failed == 0
    stored = session.execute(
        text(
            "SELECT o.status, m.delivery_status, o.provider_message_id "
            "FROM outbound_messages o JOIN messages m ON m.id=o.message_id "
            "WHERE o.id=:id"
        ),
        {"id": outbound_id},
    ).one()
    assert stored == ("delivered", "delivered", f"sandbox-{outbound_id}")
    assert session.execute(text("SELECT count(*) FROM sandbox_delivery_receipts")).scalar_one() == 1


def test_sandbox_consumer_settles_receiver_failure_then_retries(migrated_engine, session):
    _seed_channel(session)
    dispatcher_token = _dispatcher_token(session)

    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        outbound_id = _queue_outbound(
            client,
            provider="sandbox",
            external_id="local-sandbox",
            text_body="retryable consumer delivery",
        )
        receiver = _FailOnceReceiverClient(client)
        consumer = _consumer_for(client, dispatcher_token, receiver_client=receiver)
        failed = consumer.dispatch_once()
        intermediate = session.execute(
            text(
                "SELECT status, attempt_count, last_error_code "
                "FROM outbound_messages WHERE id=:id"
            ),
            {"id": outbound_id},
        ).one()

        session.execute(
            text("UPDATE outbound_messages SET next_attempt_at=now() WHERE id=:id"),
            {"id": outbound_id},
        )
        session.commit()
        succeeded = consumer.dispatch_once()
        consumer.close()

    assert failed.claimed == 1
    assert failed.delivered == 0
    assert failed.failed == 1
    assert intermediate == ("failed", 1, "SANDBOX_RECEIVER_UNAVAILABLE")
    assert succeeded.claimed == 1
    assert succeeded.delivered == 1
    assert succeeded.failed == 0
    stored = session.execute(
        text(
            "SELECT o.status, o.attempt_count, o.last_error_code, m.delivery_status "
            "FROM outbound_messages o JOIN messages m ON m.id=o.message_id "
            "WHERE o.id=:id"
        ),
        {"id": outbound_id},
    ).one()
    assert stored == ("delivered", 2, None, "delivered")


def test_sandbox_consumer_cannot_claim_test_or_whatsapp_rows(migrated_engine, session):
    _seed_channel(session, provider="test", external_id="test-only")
    _seed_channel(session, provider="whatsapp", external_id="whatsapp-only")
    _seed_channel(session, provider="sandbox", external_id="sandbox-only")
    dispatcher_token = _dispatcher_token(session)

    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        sandbox_id = _queue_outbound(
            client,
            provider="sandbox",
            external_id="sandbox-only",
            text_body="sandbox only",
        )
        test_id = _queue_outbound(
            client,
            provider="test",
            external_id="test-only",
            text_body="synthetic only",
        )
        whatsapp_id = _queue_outbound(
            client,
            provider="whatsapp",
            external_id="whatsapp-only",
            text_body="live only",
        )
        consumer = _consumer_for(client, dispatcher_token)
        result = consumer.dispatch_once()
        consumer.close()

    assert result.claimed == 1
    assert result.delivered == 1
    statuses = session.execute(
        text(
            "SELECT c.provider, o.status, m.delivery_status "
            "FROM outbound_messages o "
            "JOIN messages m ON m.id=o.message_id "
            "JOIN conversations v ON v.id=o.conversation_id "
            "JOIN channel_accounts c ON c.id=v.channel_account_id "
            "WHERE o.id IN (:sandbox_id, :test_id, :whatsapp_id) "
            "ORDER BY o.id"
        ),
        {"sandbox_id": sandbox_id, "test_id": test_id, "whatsapp_id": whatsapp_id},
    ).all()
    assert statuses == [
        ("sandbox", "delivered", "delivered"),
        ("test", "pending", "pending"),
        ("whatsapp", "pending", "pending"),
    ]


def test_sandbox_consumer_is_tenant_scoped_at_claim_and_receiver(
    migrated_engine, session
):
    other_org = create_organization(session, "Sandbox B").id
    _seed_channel(session, organization_id=other_org, external_id="sandbox-b")
    inbound = ingest_inbound_message(
        session,
        InboundMessageCreate(
            **_inbound(
                provider="sandbox",
                external_id="sandbox-b",
            )
        ),
        ctx=default_context(other_org),
    )
    outbound = enqueue_outbound_message(
        session,
        conversation_id=inbound.conversation_id,
        text_body="tenant B message",
        idempotency_key=str(uuid4()),
        ctx=default_context(other_org),
    )
    payload = session.execute(
        text("SELECT payload FROM outbound_messages WHERE id=:id"),
        {"id": outbound.outbound_id},
    ).scalar_one()
    dispatcher_token = _dispatcher_token(session)

    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        consumer = _consumer_for(client, dispatcher_token)
        claimed = consumer.dispatch_once()
        consumer.close()
        receiver = client.post(
            "/internal/sandbox/receive",
            json={"outbound_id": outbound.outbound_id, "payload": payload},
            headers=_idempotency_headers(),
        )

    assert claimed.claimed == 0
    assert receiver.status_code == 404, receiver.text
    assert session.execute(
        text("SELECT status FROM outbound_messages WHERE id=:id"),
        {"id": outbound.outbound_id},
    ).scalar_one() == "pending"
    assert session.execute(text("SELECT count(*) FROM sandbox_delivery_receipts")).scalar_one() == 0


def test_sandbox_receiver_requires_the_existing_authenticated_delivery_permission(
    migrated_engine, session
):
    _seed_channel(session)
    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = client.post(
            "/internal/sandbox/receive",
            json={"outbound_id": 1, "payload": {}},
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == 401, response.text
    assert session.execute(text("SELECT count(*) FROM sandbox_delivery_receipts")).scalar_one() == 0


def test_sandbox_consumer_configuration_fails_closed_without_local_receiver_or_credential():
    from integrations.sandbox.consumer import SandboxConfigurationError, SandboxSettings

    with pytest.raises(SandboxConfigurationError):
        SandboxSettings(
            backend_base_url="http://127.0.0.1:8000",
            receiver_url="",
            credential="dispatcher-secret",
        )
    with pytest.raises(SandboxConfigurationError):
        SandboxSettings(
            backend_base_url="http://127.0.0.1:8000",
            receiver_url="http://example.test/internal/sandbox/receive",
            credential="dispatcher-secret",
        )
    with pytest.raises(SandboxConfigurationError):
        SandboxSettings(
            backend_base_url="http://127.0.0.1:8000",
            receiver_url="http://127.0.0.1:8000/internal/sandbox/receive",
            credential="",
        )
