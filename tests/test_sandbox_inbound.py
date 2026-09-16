"""Real-PostgreSQL proofs for the controlled sandbox inbound loop."""

from __future__ import annotations

import importlib.util
import os
from uuid import uuid4

import pytest
from conftest import AUTH_HEADERS, TEST_DATABASE_URL
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.db import get_db
from app.iam.credentials import issue_credential
from app.messaging.models import ChannelAccount
from app.organization.service import create_organization
from scripts.seed_reception_demo import seed_reception_demo

ORG = 1
SANDBOX_CHANNEL = "sandbox-loop"

@pytest.fixture(scope="session")
def sandbox_agent_database_url():
    database_name = f"odonto_sandbox_inbound_{os.getpid()}_{uuid4().hex[:8]}"
    server_url = make_url(TEST_DATABASE_URL).set(database="odontoflow")
    server_engine = create_engine(
        server_url, isolation_level="AUTOCOMMIT"
    )
    with server_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    server_engine.dispose()
    yield make_url(TEST_DATABASE_URL).set(database=database_name).render_as_string(
        hide_password=False
    )
    server_engine = create_engine(
        server_url, isolation_level="AUTOCOMMIT"
    )
    with server_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    server_engine.dispose()


def _backend_app(migrated_engine):
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
    return app, maker


def _sandbox_channel(session, *, organization_id: int = ORG, external_id: str = SANDBOX_CHANNEL):
    session.add(
        ChannelAccount(
            organization_id=organization_id,
            provider="sandbox",
            external_account_id=external_id,
            phone_number_id=None,
            display_name="Controlled local sandbox",
            is_active=True,
        )
    )
    session.flush()
    session.commit()


def _token(session, *, organization_id: int, name: str, principal_type: str, profile: str):
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=organization_id,
        name=name,
        principal_type=principal_type,
    )
    _assign_profile(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        profile=profile,
    )
    _credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=name,
    )
    session.commit()
    return token


def _inbound_event(*, provider_message_id: str | None = None, channel: str = SANDBOX_CHANNEL):
    return {
        "schema_version": "1.0",
        "provider": "sandbox",
        "channel_account_external_id": channel,
        "provider_message_id": provider_message_id or f"sandbox-inbound-{uuid4()}",
        "external_contact_id": f"sandbox-contact-{uuid4()}",
        "phone_e164": "+51999000111",
        "message_type": "text",
        "text": "Book the first available cleaning",
        "occurred_at": "2026-09-07T12:00:00Z",
    }


def _auth_headers(token: str, *, idempotency: bool = True):
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency:
        headers["Idempotency-Key"] = str(uuid4())
    return headers


def _sales_settings(token: str, database_url: str):
    from sales_agent.config import AgentSettings

    return AgentSettings(
        backend_base_url="http://127.0.0.1:8000",
        backend_credential=token,
        agent_database_url=database_url,
        model="fake",
        recursion_limit=12,
        request_timeout_seconds=1.0,
    )


class _RecordingClient:
    def __init__(self, client):
        self.client = client
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((str(url), dict(kwargs.get("json", {}))))
        return self.client.post(url, **kwargs)


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None
    or importlib.util.find_spec("langgraph") is None,
    reason="Sandbox fake-model loop requires the optional sales-agent dependencies",
)
def test_sandbox_input_reaches_agent_and_delivers_one_safe_response(
    migrated_engine, session, sandbox_agent_database_url
):
    seed_reception_demo(session)
    _sandbox_channel(session)
    inbound_token = _token(
        session,
        organization_id=ORG,
        name=f"sandbox-inbound-{uuid4()}",
        principal_type="integration",
        profile="n8n-inbound",
    )
    agent_token = _token(
        session,
        organization_id=ORG,
        name=f"sandbox-agent-{uuid4()}",
        principal_type="agent",
        profile="sales-agent-v0",
    )
    dispatcher_token = _token(
        session,
        organization_id=ORG,
        name=f"sandbox-dispatcher-{uuid4()}",
        principal_type="integration",
        profile="outbound-dispatcher",
    )
    backend_app, maker = _backend_app(migrated_engine)
    observed: dict = {}

    from test_sales_agent_w4 import _scenario_model

    from integrations.sandbox.consumer import SandboxConsumer, SandboxSettings
    from integrations.sandbox.sender import SandboxInboundSender
    from sales_agent.api import create_app as create_sales_agent_app
    from sales_agent.gateway import BackendGateway
    from sales_agent.memory import PostgresAgentMemory
    from sales_agent.runtime import SalesAgentRuntime

    settings = _sales_settings(agent_token, sandbox_agent_database_url)
    with TestClient(backend_app, base_url="http://127.0.0.1:8000") as raw_backend:
        backend_client = _RecordingClient(raw_backend)
        gateway = BackendGateway(
            settings.backend_base_url,
            agent_token,
            http_client=backend_client,
        )
        with PostgresAgentMemory.open(sandbox_agent_database_url, setup=True) as memory:
            runtime = SalesAgentRuntime(
                gateway=gateway,
                model=_scenario_model(observed, same_turn_confirmation=True),
                checkpointer=memory.checkpointer,
                settings=settings,
            )
            sales_app = create_sales_agent_app(runtime=runtime, settings=settings)
            sales_app.state.auth_sessionmaker = maker
            with TestClient(sales_app) as sales_client:
                recording_sales = _RecordingClient(sales_client)
                sender = SandboxInboundSender(
                    backend_client=backend_client,
                    sales_agent_client=recording_sales,
                    inbound_token=inbound_token,
                    agent_token=agent_token,
                )
                event = _inbound_event()
                first = sender.send(event)
                replay = sender.send(event)

                consumer = SandboxConsumer(
                    SandboxSettings(
                        backend_base_url="http://127.0.0.1:8000",
                        receiver_url="http://127.0.0.1:8000/internal/sandbox/receive",
                        credential=dispatcher_token,
                    ),
                    http_client=backend_client,
                    receiver_client=backend_client,
                )
                delivered = consumer.dispatch_once()
                second_poll = consumer.dispatch_once()
                consumer.close()

    assert first.duplicate is False
    assert first.agent_response is not None
    assert first.agent_response["outcome"] == "proposed"
    assert first.agent_response["reply"]
    assert first.outbound_receipt is not None
    assert first.outbound_receipt["status"] == "pending"
    assert replay.duplicate is True
    assert replay.agent_response is None
    assert replay.outbound_receipt is None
    assert delivered.claimed == 1
    assert delivered.delivered == 1
    assert delivered.failed == 0
    assert second_poll.claimed == 0

    session.expire_all()
    stored = session.execute(
        text(
            "SELECT c.provider, i.direction, i.body_text, m.body_text, o.status, m.delivery_status "
            "FROM outbound_messages o "
            "JOIN messages m ON m.id = o.message_id "
            "JOIN conversations v ON v.id = o.conversation_id "
            "JOIN channel_accounts c ON c.id = v.channel_account_id "
            "JOIN messages i ON i.conversation_id = o.conversation_id "
            "WHERE o.id = :outbound_id AND i.direction = 'inbound'"
        ),
        {"outbound_id": first.outbound_receipt["outbound_id"]},
    ).one()
    assert stored == (
        "sandbox",
        "inbound",
        "Book the first available cleaning",
        first.agent_response["reply"],
        "delivered",
        "delivered",
    )
    assert session.execute(
        text("SELECT count(*) FROM messages WHERE direction = 'inbound'")
    ).scalar_one() == 1
    assert session.execute(
        text("SELECT count(*) FROM messages WHERE direction = 'outbound'")
    ).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM appointments")).scalar_one() == 0
    assert session.execute(
        text("SELECT count(*) FROM appointment_proposals WHERE status = 'pending'")
    ).scalar_one() == 1
    assert session.execute(
        text("SELECT count(*) FROM appointment_proposals WHERE status = 'confirmed'")
    ).scalar_one() == 0
    assert session.execute(text("SELECT count(*) FROM sandbox_delivery_receipts")).scalar_one() == 1
    assert session.execute(
        text(
            "SELECT count(*) FROM audit_events "
            "WHERE action = 'outbound.sandbox.received'"
        )
    ).scalar_one() == 1
    assert session.execute(
        text(
            "SELECT count(*) FROM audit_events "
            "WHERE entity_type = 'agent_tool' "
            "AND action = 'agent_tool.called' "
            "AND after_state->>'tool_name' = 'confirm_appointment' "
            "AND after_state->>'status' = 'error' "
            "AND after_state->>'error_code' = 'INVALID_INPUT'"
        )
    ).scalar_one() == 1
    assert [path for path, _payload in backend_client.calls].count(
        "/internal/messages/inbound"
    ) == 2
    assert sum(
        path.endswith("/sales-agent/turn") for path, _payload in recording_sales.calls
    ) == 1


def test_sandbox_sender_requires_both_server_credentials():
    from integrations.sandbox.sender import SandboxInboundSender, SandboxSenderConfigurationError

    with pytest.raises(SandboxSenderConfigurationError):
        SandboxInboundSender(
            backend_client=object(),
            sales_agent_client=object(),
            inbound_token="",
            agent_token="agent-token",
        )
    with pytest.raises(SandboxSenderConfigurationError):
        SandboxInboundSender(
            backend_client=object(),
            sales_agent_client=object(),
            inbound_token="inbound-token",
            agent_token="",
        )


@pytest.mark.parametrize("provider", ["test", "whatsapp"])
def test_sandbox_sender_rejects_non_sandbox_provider(provider):
    from integrations.sandbox.sender import (
        SandboxSenderContractError,
        normalize_sandbox_inbound,
    )

    event = _inbound_event()
    event["provider"] = provider
    with pytest.raises(SandboxSenderContractError):
        normalize_sandbox_inbound(event)


def test_sandbox_ingress_rejects_missing_invalid_and_cross_tenant_credentials(
    migrated_engine, session
):
    _sandbox_channel(session)
    other_org = create_organization(session, "Sandbox inbound other tenant").id
    _sandbox_channel(session, organization_id=other_org, external_id="sandbox-other")
    session.commit()
    backend_app, _maker = _backend_app(migrated_engine)
    event = _inbound_event(channel="sandbox-other")

    with TestClient(backend_app, raise_server_exceptions=False) as client:
        missing = client.post(
            "/internal/messages/inbound",
            json=event,
            headers=_auth_headers("", idempotency=False),
        )
        invalid = client.post(
            "/internal/messages/inbound",
            json=event,
            headers=_auth_headers("ofk_invalid"),
        )
        cross_tenant = client.post(
            "/internal/messages/inbound",
            json=event,
            headers={**AUTH_HEADERS, "Idempotency-Key": str(uuid4())},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert cross_tenant.status_code == 404
    assert session.execute(text("SELECT count(*) FROM messages")).scalar_one() == 0
