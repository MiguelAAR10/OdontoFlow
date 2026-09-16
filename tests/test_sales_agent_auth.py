"""Real-PostgreSQL authorization proofs for the Sales Agent entrypoint."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.iam.credentials import issue_credential
from app.organization.models import Organization
from sales_agent.config import AgentSettings
from sales_agent.schemas import SalesAgentTurnRequest, SalesAgentTurnResponse

ORG = 1


@dataclass
class RecordingRuntime:
    calls: list[SalesAgentTurnRequest] = field(default_factory=list)

    def turn(self, request: SalesAgentTurnRequest) -> SalesAgentTurnResponse:
        self.calls.append(request)
        return SalesAgentTurnResponse(
            conversation_id=request.conversation_id,
            latest_inbound_message_id=request.latest_inbound_message_id,
            reply="Synthetic reply",
            outcome="continue",
            handoff=False,
        )


def _token_for_principal(
    session: Session,
    *,
    organization_id: int,
    name: str,
    principal_type: str = "agent",
    profile: str | None = "sales-agent-v0",
) -> str:
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=organization_id,
        name=name,
        principal_type=principal_type,
    )
    if profile is not None:
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
    organization = Organization(name="Sales Agent auth tenant")
    session.add(organization)
    session.flush()
    return organization.id


def _settings(token: str) -> AgentSettings:
    return AgentSettings(
        backend_base_url="http://backend.test",
        backend_credential=token,
        agent_database_url=(
            "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/"
            "sales_agent_auth_test"
        ),
        model="fake",
        recursion_limit=12,
        request_timeout_seconds=1.0,
    )


def _sales_client(migrated_engine, runtime, *, configured_token: str):
    from sales_agent.api import create_app

    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    app = create_app(runtime=runtime, settings=_settings(configured_token))
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False)


def test_sales_agent_turn_rejects_missing_and_invalid_credentials_before_runtime(
    migrated_engine, session
) -> None:
    token = _token_for_principal(session, organization_id=ORG, name="turn-auth-agent")
    runtime = RecordingRuntime()

    with _sales_client(migrated_engine, runtime, configured_token=token) as client:
        missing = client.post(
            "/sales-agent/turn",
            json={"conversation_id": 7, "latest_inbound_message_id": 8},
        )
        invalid = client.post(
            "/sales-agent/turn",
            headers={"Authorization": "Bearer ofk_invalid"},
            json={"conversation_id": 7, "latest_inbound_message_id": 8},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert invalid.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert runtime.calls == []


def test_sales_agent_turn_rejects_a_valid_non_agent_principal(
    migrated_engine, session
) -> None:
    human_token = _token_for_principal(
        session,
        organization_id=ORG,
        name="turn-auth-human",
        principal_type="human",
    )
    runtime = RecordingRuntime()

    with _sales_client(
        migrated_engine, runtime, configured_token=human_token
    ) as client:
        response = client.post(
            "/sales-agent/turn",
            headers={"Authorization": f"Bearer {human_token}"},
            json={"conversation_id": 7, "latest_inbound_message_id": 8},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert runtime.calls == []


def test_sales_agent_turn_rejects_a_valid_agent_from_another_tenant(
    migrated_engine, session
) -> None:
    organization_id = _second_organization(session)
    session.commit()
    configured_token = _token_for_principal(
        session, organization_id=ORG, name="turn-auth-org-one"
    )
    other_tenant_token = _token_for_principal(
        session,
        organization_id=organization_id,
        name="turn-auth-org-two",
    )
    runtime = RecordingRuntime()

    with _sales_client(
        migrated_engine, runtime, configured_token=configured_token
    ) as client:
        response = client.post(
            "/sales-agent/turn",
            headers={"Authorization": f"Bearer {other_tenant_token}"},
            json={"conversation_id": 7, "latest_inbound_message_id": 8},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert runtime.calls == []


def test_sales_agent_turn_rejects_agent_without_entrypoint_permission(
    migrated_engine, session
) -> None:
    token = _token_for_principal(
        session,
        organization_id=ORG,
        name="turn-auth-no-permission",
        profile=None,
    )
    runtime = RecordingRuntime()

    with _sales_client(migrated_engine, runtime, configured_token=token) as client:
        response = client.post(
            "/sales-agent/turn",
            headers={"Authorization": f"Bearer {token}"},
            json={"conversation_id": 7, "latest_inbound_message_id": 8},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert runtime.calls == []


def test_sales_agent_turn_allows_the_configured_sales_agent_service(
    migrated_engine, session
) -> None:
    token = _token_for_principal(session, organization_id=ORG, name="turn-auth-valid")
    runtime = RecordingRuntime()

    with _sales_client(migrated_engine, runtime, configured_token=token) as client:
        response = client.post(
            "/sales-agent/turn",
            headers={"Authorization": f"Bearer {token}"},
            json={"conversation_id": 7, "latest_inbound_message_id": 8},
        )

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": 7,
        "latest_inbound_message_id": 8,
        "reply": "Synthetic reply",
        "outcome": "continue",
        "handoff": False,
    }
    assert [call.conversation_id for call in runtime.calls] == [7]


def test_sales_agent_turn_openapi_declares_bearer_security() -> None:
    from sales_agent.api import create_app

    schema = create_app(runtime=RecordingRuntime(), settings=_settings("ofk_configured")).openapi()

    assert schema["paths"]["/sales-agent/turn"]["post"]["security"] == [
        {"IntegrationBearer": []}
    ]
