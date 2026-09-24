"""Contract tests for truthful Sales Agent failure recovery.

These tests drive the authenticated Sales Agent HTTP boundary with synthetic
provider/runtime failures, while recovery crosses the real typed backend
gateway and writes to real PostgreSQL.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import create_app as create_backend_app
from app.agent_tools.schemas import AgentToolCall
from app.audit.models import AuditEvent
from app.db import get_db
from app.iam.credentials import issue_credential
from app.idempotency.models import CommandReceipt
from app.messaging.models import (
    ChannelAccount,
    ContactIdentity,
    Conversation,
    Message,
    OutboundMessage,
    ReceptionHandoff,
)
from app.organization.models import Organization
from sales_agent.config import AgentSettings
from sales_agent.gateway import BackendGateway
from sales_agent.runtime import (
    SalesAgentDiagnostic,
    SalesAgentExecutionError,
    SalesAgentProviderTimeout,
    SalesAgentTurnTimeout,
)
from sales_agent.schemas import SalesAgentTurnRequest

ORG = 1
FAILURE_SUMMARY = (
    "Automatic assistance could not complete the conversation and human recovery is required."
)
REQUEST_ID = "00000000-0000-4000-8000-000000000101"
CORRELATION_ID = "00000000-0000-4000-8000-000000000102"
PATIENT_TEXT_SENTINEL = "PATIENT_MESSAGE_SENTINEL"
PROVIDER_BODY_SENTINEL = "PROVIDER_RESPONSE_BODY_SENTINEL"
EXCEPTION_TEXT_SENTINEL = "EXCEPTION_TEXT_SENTINEL"
STACK_SENTINEL = "STACK_TRACE_SENTINEL"
PROMPT_SENTINEL = "PROMPT_SENTINEL"


@dataclass
class FailingRuntime:
    """A runtime seam that preserves the real API/gateway boundary."""

    gateway: BackendGateway
    failure_factory: Callable[[], BaseException]
    calls: list[SalesAgentTurnRequest] = field(default_factory=list)

    def turn(self, request: SalesAgentTurnRequest):
        self.calls.append(request)
        raise self.failure_factory()


@dataclass
class RecordingBackendClient:
    """Record typed gateway calls without replacing the real backend app."""

    client: TestClient
    calls: list[dict] = field(default_factory=list)

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.client.post(url, **kwargs)


def _token_for_principal(
    session: Session,
    *,
    organization_id: int,
    name: str,
    profile: str = "sales-agent-v0",
) -> str:
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=organization_id,
        name=name,
        principal_type="agent",
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


def _second_organization(session: Session) -> int:
    organization = session.scalar(select(Organization).where(Organization.id != ORG))
    if organization is not None:
        return organization.id
    organization = Organization(name="Sales Agent failure tenant")
    session.add(organization)
    session.flush()
    return organization.id


def _seed_conversation(
    session: Session, *, organization_id: int, suffix: str
) -> dict[str, int]:
    channel = ChannelAccount(
        organization_id=organization_id,
        provider="test",
        external_account_id=f"failure-channel-{organization_id}-{suffix}",
        phone_number_id=f"failure-phone-{organization_id}-{suffix}",
        display_name=f"Failure channel {suffix}",
        is_active=True,
    )
    session.add(channel)
    session.flush()
    contact = ContactIdentity(
        organization_id=organization_id,
        channel_account_id=channel.id,
        external_contact_id=f"failure-contact-{organization_id}-{suffix}",
        normalized_phone_e164=(
            f"+51999{organization_id}{abs(hash(suffix)) % 10_000_000:07d}"
        ),
        consent_status="opted_in",
    )
    session.add(contact)
    session.flush()
    conversation = Conversation(
        organization_id=organization_id,
        channel_account_id=channel.id,
        contact_identity_id=contact.id,
        status="open",
        last_message_at=datetime.now(UTC),
    )
    session.add(conversation)
    session.flush()
    message = Message(
        organization_id=organization_id,
        channel_account_id=channel.id,
        conversation_id=conversation.id,
        direction="inbound",
        provider_message_id=f"failure-message-{organization_id}-{suffix}",
        message_type="text",
        body_text=PATIENT_TEXT_SENTINEL,
        media_reference=None,
        delivery_status="received",
        occurred_at=datetime.now(UTC),
        content_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(message)
    session.commit()
    return {
        "channel_id": channel.id,
        "contact_id": contact.id,
        "conversation_id": conversation.id,
        "message_id": message.id,
    }


def _settings(token: str) -> AgentSettings:
    return AgentSettings(
        backend_base_url="http://backend.test",
        backend_credential=token,
        agent_database_url=(
            "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/"
            "sales_agent_failure_handoff_test"
        ),
        model="fake",
        recursion_limit=12,
        request_timeout_seconds=1.0,
    )


def _backend_client(migrated_engine) -> tuple[TestClient, sessionmaker]:
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    app = create_backend_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False), maker


def _sales_client(
    migrated_engine,
    *,
    token: str,
    gateway_client: RecordingBackendClient,
    failure_factory: Callable[[], BaseException],
) -> tuple[TestClient, FailingRuntime]:
    from sales_agent.api import create_app as create_sales_agent_app

    gateway = BackendGateway(
        "http://backend.test",
        token,
        http_client=gateway_client,
    )
    runtime = FailingRuntime(gateway=gateway, failure_factory=failure_factory)
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    app = create_sales_agent_app(runtime=runtime, settings=_settings(token))
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False), runtime


def _failure_factory(kind: str) -> Callable[[], BaseException]:
    def build() -> BaseException:
        diagnostic = SalesAgentDiagnostic(
            stage="model_execution",
            category={
                "provider_timeout": "provider_timeout",
                "turn_timeout": "turn_timeout",
                "runtime_exception": "unknown",
            }[kind],
            upstream_status=504,
            upstream_request_id="upstream-request-123",
            partial_business_effects=False,
        )
        message = (
            f"{PROVIDER_BODY_SENTINEL} {EXCEPTION_TEXT_SENTINEL} "
            f"{STACK_SENTINEL} {PROMPT_SENTINEL}"
        )
        exception_type = {
            "provider_timeout": SalesAgentProviderTimeout,
            "turn_timeout": SalesAgentTurnTimeout,
            "runtime_exception": SalesAgentExecutionError,
        }[kind]
        return exception_type(message, diagnostic=diagnostic)

    return build


def _excluded_category_failure_factory(
    category: str, *, upstream_status: int
) -> Callable[[], BaseException]:
    def build() -> BaseException:
        diagnostic = SalesAgentDiagnostic(
            stage="model_execution",
            category=category,
            upstream_status=upstream_status,
            upstream_request_id="upstream-request-123",
            partial_business_effects=False,
        )
        return SalesAgentExecutionError(
            f"{PROVIDER_BODY_SENTINEL} {EXCEPTION_TEXT_SENTINEL}",
            diagnostic=diagnostic,
        )

    return build


def _turn(
    client: TestClient,
    *,
    token: str,
    conversation_id: int,
    attempt: int = 1,
):
    request_id = str(UUID(int=UUID(REQUEST_ID).int + attempt - 1))
    correlation_id = str(UUID(int=UUID(CORRELATION_ID).int + attempt - 1))
    return client.post(
        "/sales-agent/turn",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        },
        json={
            "conversation_id": conversation_id,
            "latest_inbound_message_id": 1,
        },
    )


def _assert_public_failure(
    response, *, request_id: str, correlation_id: str
) -> None:
    assert response.status_code == 503, response.text
    assert response.json() == {
        "error": {
            "code": "AGENT_EXECUTION_FAILED",
            "message": "The Sales Agent could not complete this turn safely.",
            "details": {
                "request_id": request_id,
                "correlation_id": correlation_id,
            },
        }
    }


def _handoffs(session: Session, *, organization_id: int, conversation_id: int):
    return session.scalars(
        select(ReceptionHandoff).where(
            ReceptionHandoff.organization_id == organization_id,
            ReceptionHandoff.conversation_id == conversation_id,
            ReceptionHandoff.status.in_(("pending", "claimed")),
        )
    ).all()


def _typed_tool_call(
    client: TestClient,
    *,
    token: str,
    conversation_id: int,
    tool_name: str,
    arguments: dict,
):
    request_id = uuid4()
    correlation_id = uuid4()
    key = uuid4() if tool_name != "get_reception_context" else None
    payload = AgentToolCall(
        tool_version="1.1" if key is not None else "1.0",
        tool_name=tool_name,
        conversation_id=conversation_id,
        request_id=request_id,
        correlation_id=correlation_id,
        idempotency_key=key,
        arguments=arguments,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-Id": str(request_id),
        "X-Correlation-Id": str(correlation_id),
    }
    if key is not None:
        headers["Idempotency-Key"] = str(key)
    return client.post(
        "/agent-tools/call",
        headers=headers,
        json=payload.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "kind",
    ["provider_timeout", "turn_timeout", "runtime_exception"],
)
def test_authenticated_provider_runtime_failure_creates_one_scoped_handoff(
    migrated_engine, session, caplog, kind
) -> None:
    """A1/A3/A5: every included failure class reaches one safe handoff."""

    token = _token_for_principal(
        session,
        organization_id=ORG,
        name=f"failure-boundary-{kind}",
    )
    seeded = _seed_conversation(session, organization_id=ORG, suffix=kind)
    backend_client, _maker = _backend_client(migrated_engine)
    recording_client = RecordingBackendClient(backend_client)
    caplog.set_level(logging.WARNING, logger="sales_agent.diagnostics")

    with backend_client:
        sales_client, runtime = _sales_client(
            migrated_engine,
            token=token,
            gateway_client=recording_client,
            failure_factory=_failure_factory(kind),
        )
        with sales_client:
            response = _turn(
                sales_client,
                token=token,
                conversation_id=seeded["conversation_id"],
            )
        blocked_read = _typed_tool_call(
            backend_client,
            token=token,
            conversation_id=seeded["conversation_id"],
            tool_name="get_reception_context",
            arguments={},
        )

    _assert_public_failure(
        response,
        request_id=str(UUID(REQUEST_ID)),
        correlation_id=str(UUID(CORRELATION_ID)),
    )
    assert len(runtime.calls) == 1
    assert len(recording_client.calls) == 1
    handoff_call = recording_client.calls[0]
    assert handoff_call["json"]["tool_name"] == "request_human_handoff"
    assert UUID(handoff_call["json"]["idempotency_key"]).version == 4
    assert (
        handoff_call["headers"]["Idempotency-Key"]
        == handoff_call["json"]["idempotency_key"]
    )
    assert blocked_read.status_code == 200
    assert blocked_read.json()["status"] == "error"
    assert blocked_read.json()["error"]["code"] == "ENTITY_INACTIVE"

    session.expire_all()
    handoffs = _handoffs(
        session,
        organization_id=ORG,
        conversation_id=seeded["conversation_id"],
    )
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert (handoff.organization_id, handoff.conversation_id) == (
        ORG,
        seeded["conversation_id"],
    )
    assert handoff.contact_identity_id == seeded["contact_id"]
    assert handoff.status == "pending"
    assert handoff.reason_code == "other"
    assert handoff.reason_summary == FAILURE_SUMMARY
    conversation = session.get(Conversation, seeded["conversation_id"])
    assert conversation is not None
    assert conversation.status == "human_handoff"

    receipts = session.scalars(
        select(CommandReceipt).where(
            CommandReceipt.organization_id == ORG,
            CommandReceipt.operation == "conversations.request_handoff",
        )
    ).all()
    assert len(receipts) == 1
    assert receipts[0].resource_type == "reception_handoff"
    assert receipts[0].resource_id == str(handoff.id)
    assert UUID(receipts[0].idempotency_key).version == 4
    domain_audits = session.scalars(
        select(AuditEvent).where(
            AuditEvent.organization_id == ORG,
            AuditEvent.entity_type == "conversation",
            AuditEvent.entity_id == str(seeded["conversation_id"]),
            AuditEvent.action == "conversation.human_handoff_requested",
        )
    ).all()
    assert len(domain_audits) == 1
    assert session.scalar(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.organization_id == ORG,
            OutboundMessage.conversation_id == seeded["conversation_id"],
        )
    ) == 0

    records = [
        record
        for record in caplog.records
        if record.name == "sales_agent.diagnostics"
    ]
    assert len(records) == 1
    logged = json.loads(records[0].getMessage().removeprefix("sales_agent_failure "))
    assert logged["stage"] == "model_execution"
    assert logged["category"] in {"provider_timeout", "turn_timeout", "unknown"}
    assert logged["upstream_status"] == 504
    assert logged["upstream_request_id"] == "upstream-request-123"
    assert logged["partial_business_effects"] is False
    metadata = json.dumps(
        {
            "handoff": handoff.__dict__,
            "receipt": receipts[0].outcome_json,
            "audit": domain_audits[0].after_state,
            "response": response.json(),
            "diagnostic": logged,
        },
        default=str,
    )
    for forbidden in (
        PROVIDER_BODY_SENTINEL,
        EXCEPTION_TEXT_SENTINEL,
        STACK_SENTINEL,
        PROMPT_SENTINEL,
        PATIENT_TEXT_SENTINEL,
        token,
    ):
        assert forbidden not in metadata


@pytest.mark.parametrize(
    "category,upstream_status",
    [
        ("provider_authentication", 401),
        ("provider_rate_limited", 429),
    ],
)
def test_excluded_provider_failure_category_creates_no_handoff(
    migrated_engine, session, category, upstream_status
) -> None:
    """D1 finding 1: only the owner-approved categories may create recovery."""

    token = _token_for_principal(
        session,
        organization_id=ORG,
        name=f"failure-excluded-{category}",
    )
    seeded = _seed_conversation(session, organization_id=ORG, suffix=category)
    backend_client, _maker = _backend_client(migrated_engine)
    recording_client = RecordingBackendClient(backend_client)
    with backend_client:
        sales_client, runtime = _sales_client(
            migrated_engine,
            token=token,
            gateway_client=recording_client,
            failure_factory=_excluded_category_failure_factory(
                category, upstream_status=upstream_status
            ),
        )
        with sales_client:
            response = _turn(
                sales_client,
                token=token,
                conversation_id=seeded["conversation_id"],
            )

    _assert_public_failure(
        response,
        request_id=str(UUID(REQUEST_ID)),
        correlation_id=str(UUID(CORRELATION_ID)),
    )
    assert len(runtime.calls) == 1
    assert len(recording_client.calls) == 0

    session.expire_all()
    assert (
        _handoffs(
            session,
            organization_id=ORG,
            conversation_id=seeded["conversation_id"],
        )
        == []
    )
    conversation = session.get(Conversation, seeded["conversation_id"])
    assert conversation is not None
    assert conversation.status == "open"


def test_repeated_typed_handoff_request_reuses_the_same_handoff(
    migrated_engine, session
) -> None:
    """D1 finding 2: a second typed request_human_handoff call reuses the open row."""

    token = _token_for_principal(
        session,
        organization_id=ORG,
        name="failure-typed-repeat-agent",
    )
    seeded = _seed_conversation(session, organization_id=ORG, suffix="typed-repeat")
    backend_client, _maker = _backend_client(migrated_engine)

    with backend_client:
        first = _typed_tool_call(
            backend_client,
            token=token,
            conversation_id=seeded["conversation_id"],
            tool_name="request_human_handoff",
            arguments={"reason_code": "other", "reason_summary": FAILURE_SUMMARY},
        )
        second = _typed_tool_call(
            backend_client,
            token=token,
            conversation_id=seeded["conversation_id"],
            tool_name="request_human_handoff",
            arguments={"reason_code": "other", "reason_summary": FAILURE_SUMMARY},
        )

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "success", first.text
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "success", second.text
    first_handoff = first.json()["data"]["handoff"]
    second_handoff = second.json()["data"]["handoff"]
    assert first_handoff["handoff_id"] == second_handoff["handoff_id"]
    assert first_handoff["resource_id"] == second_handoff["resource_id"]

    session.expire_all()
    handoffs = _handoffs(
        session,
        organization_id=ORG,
        conversation_id=seeded["conversation_id"],
    )
    assert len(handoffs) == 1
    handoff_id = handoffs[0].id
    conversation = session.get(Conversation, seeded["conversation_id"])
    assert conversation is not None
    assert conversation.status == "human_handoff"

    receipts = session.scalars(
        select(CommandReceipt).where(
            CommandReceipt.organization_id == ORG,
            CommandReceipt.operation == "conversations.request_handoff",
        )
    ).all()
    assert {receipt.resource_id for receipt in receipts} == {str(handoff_id)}
    domain_audits = session.scalars(
        select(AuditEvent).where(
            AuditEvent.organization_id == ORG,
            AuditEvent.entity_type == "conversation",
            AuditEvent.entity_id == str(seeded["conversation_id"]),
            AuditEvent.action == "conversation.human_handoff_requested",
        )
    ).all()
    assert {audit.after_state["handoff_id"] for audit in domain_audits} == {handoff_id}
    assert (
        session.scalar(
            select(func.count()).select_from(OutboundMessage).where(
                OutboundMessage.organization_id == ORG,
                OutboundMessage.conversation_id == seeded["conversation_id"],
            )
        )
        == 0
    )


def test_repeated_provider_failure_reuses_the_same_open_handoff(
    migrated_engine, session
) -> None:
    """A2: repeat failures do not create a second open recovery state."""

    token = _token_for_principal(
        session,
        organization_id=ORG,
        name="failure-repeat-agent",
    )
    seeded = _seed_conversation(session, organization_id=ORG, suffix="repeat")
    backend_client, _maker = _backend_client(migrated_engine)
    recording_client = RecordingBackendClient(backend_client)
    with backend_client:
        sales_client, runtime = _sales_client(
            migrated_engine,
            token=token,
            gateway_client=recording_client,
            failure_factory=_failure_factory("runtime_exception"),
        )
        with sales_client:
            first = _turn(
                sales_client,
                token=token,
                conversation_id=seeded["conversation_id"],
                attempt=1,
            )
            second = _turn(
                sales_client,
                token=token,
                conversation_id=seeded["conversation_id"],
                attempt=2,
            )

    _assert_public_failure(
        first,
        request_id=str(UUID(REQUEST_ID)),
        correlation_id=str(UUID(CORRELATION_ID)),
    )
    _assert_public_failure(
        second,
        request_id=str(UUID(int=UUID(REQUEST_ID).int + 1)),
        correlation_id=str(UUID(int=UUID(CORRELATION_ID).int + 1)),
    )
    assert len(runtime.calls) == 2
    assert len(recording_client.calls) == 2
    assert all(
        call["json"]["tool_name"] == "request_human_handoff"
        for call in recording_client.calls
    )
    assert all(
        UUID(call["json"]["idempotency_key"]).version == 4
        for call in recording_client.calls
    )

    session.expire_all()
    handoffs = _handoffs(
        session,
        organization_id=ORG,
        conversation_id=seeded["conversation_id"],
    )
    assert len(handoffs) == 1
    assert handoffs[0].reason_code == "other"
    assert handoffs[0].reason_summary == FAILURE_SUMMARY
    assert session.get(Conversation, seeded["conversation_id"]).status == "human_handoff"
    assert session.scalar(
        select(func.count()).select_from(ReceptionHandoff).where(
            ReceptionHandoff.organization_id == ORG,
            ReceptionHandoff.conversation_id == seeded["conversation_id"],
            ReceptionHandoff.status.in_(("pending", "claimed")),
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.organization_id == ORG,
            OutboundMessage.conversation_id == seeded["conversation_id"],
        )
    ) == 0


def test_failure_after_prior_typed_tool_effect_has_no_duplicate_business_mutation(
    migrated_engine, session
) -> None:
    """A4: a committed typed mutation remains singular when the turn fails."""

    token = _token_for_principal(
        session,
        organization_id=ORG,
        name="failure-prior-effect-agent",
        profile="conversation-agent",
    )
    seeded = _seed_conversation(session, organization_id=ORG, suffix="prior-effect")
    backend_client, _maker = _backend_client(migrated_engine)
    recording_client = RecordingBackendClient(backend_client)
    with backend_client:
        prior = _typed_tool_call(
            backend_client,
            token=token,
            conversation_id=seeded["conversation_id"],
            tool_name="register_contact_profile",
            arguments={"full_name": "Prior committed profile"},
        )
        assert prior.status_code == 200, prior.text
        assert prior.json()["status"] == "success", prior.text

        sales_client, runtime = _sales_client(
            migrated_engine,
            token=token,
            gateway_client=recording_client,
            failure_factory=_failure_factory("runtime_exception"),
        )
        with sales_client:
            response = _turn(
                sales_client,
                token=token,
                conversation_id=seeded["conversation_id"],
            )

    _assert_public_failure(
        response,
        request_id=str(UUID(REQUEST_ID)),
        correlation_id=str(UUID(CORRELATION_ID)),
    )
    assert len(runtime.calls) == 1
    assert len(recording_client.calls) == 1
    assert recording_client.calls[0]["json"]["tool_name"] == "request_human_handoff"

    session.expire_all()
    assert session.scalar(
        select(func.count()).select_from(Conversation).where(
            Conversation.organization_id == ORG,
            Conversation.id == seeded["conversation_id"],
            Conversation.status == "human_handoff",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(ReceptionHandoff).where(
            ReceptionHandoff.organization_id == ORG,
            ReceptionHandoff.conversation_id == seeded["conversation_id"],
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.organization_id == ORG,
            OutboundMessage.conversation_id == seeded["conversation_id"],
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.organization_id == ORG,
            AuditEvent.entity_type == "contact_profile",
            AuditEvent.action == "contact_profile.registered",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.organization_id == ORG,
            AuditEvent.entity_type == "conversation",
            AuditEvent.entity_id == str(seeded["conversation_id"]),
            AuditEvent.action == "conversation.human_handoff_requested",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(CommandReceipt).where(
            CommandReceipt.organization_id == ORG,
            CommandReceipt.operation == "contact_profiles.register",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(CommandReceipt).where(
            CommandReceipt.organization_id == ORG,
            CommandReceipt.operation == "conversations.request_handoff",
        )
    ) == 1


def test_failure_handoff_is_created_and_read_only_within_authenticated_tenant(
    migrated_engine, session
) -> None:
    """A6: equivalent failures stay isolated to each credential-derived tenant."""

    other_org = _second_organization(session)
    session.commit()
    token_a = _token_for_principal(
        session,
        organization_id=ORG,
        name="failure-tenant-a",
    )
    token_b = _token_for_principal(
        session,
        organization_id=other_org,
        name="failure-tenant-b",
    )
    seeded_a = _seed_conversation(session, organization_id=ORG, suffix="tenant-a")
    seeded_b = _seed_conversation(session, organization_id=other_org, suffix="tenant-b")
    backend_client, _maker = _backend_client(migrated_engine)
    recording_a = RecordingBackendClient(backend_client)
    recording_b = RecordingBackendClient(backend_client)

    with backend_client:
        sales_a, _runtime_a = _sales_client(
            migrated_engine,
            token=token_a,
            gateway_client=recording_a,
            failure_factory=_failure_factory("runtime_exception"),
        )
        sales_b, _runtime_b = _sales_client(
            migrated_engine,
            token=token_b,
            gateway_client=recording_b,
            failure_factory=_failure_factory("runtime_exception"),
        )
        with sales_a, sales_b:
            response_a = _turn(
                sales_a,
                token=token_a,
                conversation_id=seeded_a["conversation_id"],
            )
            response_b = _turn(
                sales_b,
                token=token_b,
                conversation_id=seeded_b["conversation_id"],
            )

        cross_read = _typed_tool_call(
            backend_client,
            token=token_a,
            conversation_id=seeded_b["conversation_id"],
            tool_name="get_reception_context",
            arguments={},
        )
        cross_write = _typed_tool_call(
            backend_client,
            token=token_a,
            conversation_id=seeded_b["conversation_id"],
            tool_name="request_human_handoff",
            arguments={
                "reason_code": "other",
                "reason_summary": FAILURE_SUMMARY,
            },
        )

    for response in (response_a, response_b):
        _assert_public_failure(
            response,
            request_id=str(UUID(REQUEST_ID)),
            correlation_id=str(UUID(CORRELATION_ID)),
        )
    assert cross_read.status_code == 200
    assert cross_read.json()["status"] == "error"
    assert cross_read.json()["error"]["code"] == "NOT_FOUND"
    assert FAILURE_SUMMARY not in cross_read.text
    assert cross_write.status_code == 200
    assert cross_write.json()["status"] == "error"
    assert cross_write.json()["error"]["code"] == "NOT_FOUND"
    assert FAILURE_SUMMARY not in cross_write.text

    session.expire_all()
    handoffs_a = _handoffs(
        session,
        organization_id=ORG,
        conversation_id=seeded_a["conversation_id"],
    )
    handoffs_b = _handoffs(
        session,
        organization_id=other_org,
        conversation_id=seeded_b["conversation_id"],
    )
    assert len(handoffs_a) == len(handoffs_b) == 1
    assert handoffs_a[0].organization_id == ORG
    assert handoffs_b[0].organization_id == other_org
    assert (
        handoffs_a[0].reason_summary
        == handoffs_b[0].reason_summary
        == FAILURE_SUMMARY
    )
    assert session.scalar(
        select(func.count()).select_from(ReceptionHandoff).where(
            ReceptionHandoff.organization_id == ORG,
            ReceptionHandoff.conversation_id == seeded_b["conversation_id"],
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(ReceptionHandoff).where(
            ReceptionHandoff.organization_id == other_org,
            ReceptionHandoff.conversation_id == seeded_a["conversation_id"],
        )
    ) == 0


def test_handoff_write_failure_preserves_original_503_and_claims_no_recovery(
    migrated_engine, session, monkeypatch, caplog
) -> None:
    """A7: an uncommitted handoff cannot be reported as durable recovery."""

    from app.agent_tools import service as agent_tool_service
    from app.audit.service import record_event
    from app.iam.service import require_permission
    from app.idempotency.service import (
        IdempotencyClaim,
        claim_receipt,
        command_fingerprint,
    )

    token = _token_for_principal(
        session,
        organization_id=ORG,
        name="failure-write-error-agent",
    )
    seeded = _seed_conversation(session, organization_id=ORG, suffix="write-error")
    backend_client, _maker = _backend_client(migrated_engine)
    recording_client = RecordingBackendClient(backend_client)
    attempts = 0

    def fail_before_commit(session, *, call, arguments, ctx):
        del arguments
        nonlocal attempts
        attempts += 1
        claim = IdempotencyClaim(
            operation="conversations.request_handoff",
            key=str(call.idempotency_key),
            fingerprint=command_fingerprint(
                operation="conversations.request_handoff",
                organization_id=ctx.organization_id,
                params={"conversation_id": call.conversation_id},
            ),
        )
        with session.begin():
            receipt = claim_receipt(session, ctx, claim)
            require_permission(session, ctx, "conversations.manage")
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.organization_id == ctx.organization_id,
                    Conversation.id == call.conversation_id,
                )
            )
            contact = session.scalar(
                select(ContactIdentity).where(
                    ContactIdentity.organization_id == ctx.organization_id,
                    ContactIdentity.id == conversation.contact_identity_id,
                )
            )
            session.add(
                ReceptionHandoff(
                    organization_id=ctx.organization_id,
                    conversation_id=conversation.id,
                    contact_identity_id=contact.id,
                    reason_code="other",
                    reason_summary=FAILURE_SUMMARY,
                    status="pending",
                )
            )
            conversation.status = "human_handoff"
            record_event(
                session,
                ctx=ctx,
                entity_type="conversation",
                entity_id=str(conversation.id),
                action="conversation.human_handoff_requested",
                after_state={
                    "handoff_id": -1,
                    "reason_code": "other",
                    "status": "pending",
                },
            )
            session.flush()
            assert receipt is not None
            raise RuntimeError(f"{EXCEPTION_TEXT_SENTINEL} before commit")

    monkeypatch.setattr(agent_tool_service, "run_handoff_tool", fail_before_commit)
    caplog.set_level(logging.WARNING, logger="sales_agent.diagnostics")
    with backend_client:
        sales_client, runtime = _sales_client(
            migrated_engine,
            token=token,
            gateway_client=recording_client,
            failure_factory=_failure_factory("runtime_exception"),
        )
        with sales_client:
            response = _turn(
                sales_client,
                token=token,
                conversation_id=seeded["conversation_id"],
            )

    _assert_public_failure(
        response,
        request_id=str(UUID(REQUEST_ID)),
        correlation_id=str(UUID(CORRELATION_ID)),
    )
    assert len(runtime.calls) == 1
    assert attempts == 1
    assert len(recording_client.calls) == 1
    assert recording_client.calls[0]["json"]["tool_name"] == "request_human_handoff"
    session.expire_all()
    assert _handoffs(
        session,
        organization_id=ORG,
        conversation_id=seeded["conversation_id"],
    ) == []
    assert session.scalar(
        select(func.count()).select_from(CommandReceipt).where(
            CommandReceipt.organization_id == ORG,
            CommandReceipt.operation == "conversations.request_handoff",
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.organization_id == ORG,
            AuditEvent.entity_type == "conversation",
            AuditEvent.entity_id == str(seeded["conversation_id"]),
            AuditEvent.action == "conversation.human_handoff_requested",
        )
    ) == 0
    assert session.get(Conversation, seeded["conversation_id"]).status == "open"
    assert session.scalar(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.organization_id == ORG,
            OutboundMessage.conversation_id == seeded["conversation_id"],
        )
    ) == 0
    assert EXCEPTION_TEXT_SENTINEL not in response.text
    assert EXCEPTION_TEXT_SENTINEL not in caplog.text
