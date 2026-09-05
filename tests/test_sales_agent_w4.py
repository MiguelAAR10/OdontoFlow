"""W4 repository-backed WF-01 contract and synthetic integration tests."""

from __future__ import annotations

import json
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_EXPORT = (
    REPO_ROOT / "integrations" / "n8n" / "workflows" / "WF-01-sales-agent-v0.json"
)


@pytest.fixture(scope="session")
def w4_agent_database_url():
    from conftest import TEST_DATABASE_URL

    database_name = f"odonto_w4_agent_{os.getpid()}_{uuid4().hex[:8]}"
    server_url = make_url(TEST_DATABASE_URL).set(database="odontoflow")
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    server_engine.dispose()
    yield make_url(TEST_DATABASE_URL).set(database=database_name).render_as_string(
        hide_password=False
    )
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    server_engine.dispose()


def _synthetic_event(message_id: str, contact_id: str, text_value: str) -> dict[str, str]:
    return {
        "message_id": message_id,
        "contact_id": contact_id,
        "phone_e164": "+51999000001" if contact_id == "contact-a" else "+51999000002",
        "text": text_value,
        "occurred_at": "2026-09-05T12:00:00Z",
        "provider": "test",
        "channel_account_external_id": "odonto-smart-lab",
    }


def _scenario_model(observed: dict[str, Any]):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class ScenarioModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

        @staticmethod
        def _decode_tool_message(message):
            content = message.content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    return None
            return content if isinstance(content, dict) else None

        @classmethod
        def _tool_result(cls, messages, name):
            for message in reversed(messages):
                if message.type != "tool" or getattr(message, "name", None) != name:
                    continue
                return cls._decode_tool_message(message)
            return None

        @staticmethod
        def _call(name: str, args: dict[str, Any], sequence: int) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": f"wf4-synthetic-{sequence}",
                    }
                ],
            )

        @staticmethod
        def _response(reply: str, outcome: str) -> AIMessage:
            return ScenarioModel._call(
                "SalesAgentResponse",
                {"reply": reply, "outcome": outcome, "handoff": outcome == "handoff"},
                999,
            )

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            humans = [message for message in messages if message.type == "human"]
            if not humans:
                raise AssertionError("scenario model received no inbound text")
            turn = len(humans)
            current = messages[max(index for index, item in enumerate(messages) if item.type == "human") + 1 :]
            tool_messages = [message for message in current if message.type == "tool"]
            last_tool = getattr(tool_messages[-1], "name", None) if tool_messages else None
            sequence = len([message for message in messages if message.type == "ai"]) + 1

            if turn in {1, 4}:
                if not tool_messages:
                    return ChatResult(
                        generations=[
                            ChatGeneration(
                                message=self._call("list_services", {}, sequence)
                            )
                        ]
                    )
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=self._response(
                                "Puedo ayudarte con la limpieza. ¿Qué sede y horario prefieres?",
                                "continue",
                            )
                        )
                    ]
                )

            if turn in {2, 5}:
                if not tool_messages:
                    message = self._call("list_locations", {}, sequence)
                elif last_tool == "list_locations":
                    services_result = self._tool_result(messages, "list_services")
                    locations_result = self._decode_tool_message(tool_messages[-1])
                    services = (services_result or {}).get("data", {}).get("services", [])
                    locations = (locations_result or {}).get("data", {}).get("locations", [])
                    service = next(row for row in services if row["name"] == "Limpieza dental")
                    location = next(row for row in locations if "Lince" in row["name"])
                    message = self._call(
                        "query_available_slots",
                        {
                            "service_id": service["id"],
                            "location_id": location["id"],
                            "window_start": "2026-09-07T13:00:00Z",
                            "window_end": "2026-09-07T15:00:00Z",
                        },
                        sequence,
                    )
                elif last_tool == "query_available_slots":
                    result = self._decode_tool_message(tool_messages[-1]) or {}
                    slots = result.get("data", {}).get("slots", [])
                    assert slots
                    selected = slots[0]
                    observed.setdefault("slots", []).append(selected)
                    services_result = self._tool_result(messages, "list_services") or {}
                    locations_result = self._tool_result(messages, "list_locations") or {}
                    service = next(
                        row
                        for row in services_result.get("data", {}).get("services", [])
                        if row["name"] == "Limpieza dental"
                    )
                    location = next(
                        row
                        for row in locations_result.get("data", {}).get("locations", [])
                        if "Lince" in row["name"]
                    )
                    message = self._call(
                        "propose_appointment",
                        {
                            "full_name": "Synthetic Patient A",
                            "service_id": service["id"],
                            "location_id": location["id"],
                            "practitioner_id": selected["practitioner_id"],
                            "start": selected["start"],
                        },
                        sequence,
                    )
                else:
                    proposal = (self._decode_tool_message(tool_messages[-1]) or {}).get(
                        "data", {}
                    ).get("proposal")
                    assert proposal
                    observed.setdefault("proposals", []).append(proposal)
                    message = self._response(
                        "Tengo este horario disponible. ¿Deseas confirmarlo?", "proposed"
                    )
            elif turn in {3, 6}:
                latest_text = str(humans[-1].content).lower()
                if "yes confirm" not in latest_text:
                    message = self._response(
                        "Quedo pendiente de tu confirmación explícita.", "proposed"
                    )
                elif not tool_messages:
                    message = self._call("get_reception_context", {}, sequence)
                elif last_tool == "get_reception_context":
                    context = self._decode_tool_message(tool_messages[-1]) or {}
                    pending = (context.get("data", {}) or {}).get("pending_action")
                    assert pending and pending["action_type"] == "BOOK"
                    message = self._call(
                        "confirm_appointment",
                        {
                            "proposal_id": pending["proposal_id"],
                            "confirmation_token": pending["confirmation_token"],
                        },
                        sequence,
                    )
                else:
                    appointment = (self._decode_tool_message(tool_messages[-1]) or {}).get(
                        "data", {}
                    ).get("appointment")
                    assert appointment
                    observed.setdefault("appointments", []).append(appointment)
                    message = self._response("Tu cita quedó confirmada.", "confirmed")
            else:
                message = self._response("Puedo seguir ayudándote.", "continue")

            return ChatResult(generations=[ChatGeneration(message=message)])

    return ScenarioModel(messages=iter(()))


def test_wf01_export_is_versioned_and_uses_only_http_or_control_nodes() -> None:
    assert WORKFLOW_EXPORT.is_file()
    workflow = json.loads(WORKFLOW_EXPORT.read_text(encoding="utf-8"))

    assert workflow["name"] == "WF-01 Sales Agent V0 — Synthetic Inbound Loop"
    assert len(workflow["description"]) > 80
    assert workflow["active"] is False
    assert workflow["meta"]["workflow_key"] == "WF-01"
    assert workflow["meta"]["workflow_version"] == "0.1.0"
    assert workflow["meta"]["provider"] == "test"

    node_types = {node["type"] for node in workflow["nodes"]}
    assert not any("postgres" in node_type.lower() for node_type in node_types)
    assert not any("mysql" in node_type.lower() for node_type in node_types)
    assert not any("mongodb" in node_type.lower() for node_type in node_types)

    node_names = {node["name"] for node in workflow["nodes"]}
    assert {
        "Synthetic/Test inbound trigger",
        "Normalize test inbound payload",
        "Conversation-scoped debounce buffer",
        "Persist inbound message",
        "Call Sales Agent turn",
        "Persist outbound response",
        "Return test-provider result",
    }.issubset(node_names)

    serialized = json.dumps(workflow).lower()
    assert "promotions" not in serialized
    assert "base_price" not in serialized
    assert "password" not in serialized
    assert "postgres" not in serialized


def test_wf01_export_keeps_http_order_and_retries_transport_only() -> None:
    workflow = json.loads(WORKFLOW_EXPORT.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in workflow["nodes"]}

    assert nodes["Persist inbound message"]["parameters"]["url"].endswith(
        "/internal/messages/inbound"
    )
    assert nodes["Call Sales Agent turn"]["parameters"]["url"].endswith(
        "/sales-agent/turn"
    )
    assert nodes["Persist outbound response"]["parameters"]["url"].endswith(
        "/outbound"
    )
    assert nodes["Persist inbound message"]["retryOnFail"] is True
    assert nodes["Persist inbound message"]["maxTries"] == 3
    assert nodes["Call Sales Agent turn"].get("retryOnFail", False) is False
    assert nodes["Persist outbound response"]["retryOnFail"] is True
    assert nodes["Persist outbound response"]["maxTries"] == 3
    assert nodes["Scheduled follow-up shape (disabled)"]["disabled"] is True

    connections = workflow["connections"]
    assert connections["Flush ready conversation buffer"]["main"][0][0]["node"] == (
        "Persist inbound message"
    )
    assert connections["Persist inbound message"]["main"][0][0]["node"] == (
        "Stop duplicate business action"
    )
    assert connections["Stop duplicate business action"]["main"][0][0]["node"] == (
        "Return duplicate inbound result"
    )
    assert connections["Stop duplicate business action"]["main"][1][0]["node"] == (
        "Call Sales Agent turn"
    )
    assert connections["Call Sales Agent turn"]["main"][0][0]["node"] == (
        "Prepare outbound transport identity"
    )
    outbound_headers = nodes["Persist outbound response"]["parameters"][
        "headerParameters"
    ]["parameters"]
    outbound_key_header = next(
        header for header in outbound_headers if header["name"] == "Idempotency-Key"
    )
    assert "outbound_idempotency_key" in outbound_key_header["value"]


def test_transport_retry_reuses_the_same_event_identity() -> None:
    import httpx

    from integrations.n8n.wf_01_sales_agent_v0 import _post_json

    class FlakyClient:
        def __init__(self):
            self.headers: list[dict[str, str]] = []
            self.calls = 0

        def post(self, url, **kwargs):
            self.calls += 1
            self.headers.append(dict(kwargs["headers"]))
            if self.calls == 1:
                return httpx.Response(503)
            return httpx.Response(201, json={"accepted": True})

    client = FlakyClient()
    payload = _post_json(
        client,
        "/internal/messages/inbound",
        {"provider_message_id": "synthetic-retry"},
        headers={"Idempotency-Key": "fixed-event-key"},
    )

    assert payload == {"accepted": True}
    assert client.calls == 2
    assert client.headers == [client.headers[0], client.headers[0]]


def test_n8n_agent_credential_uses_the_seven_tool_sales_agent_profile(session) -> None:
    from app.iam.models import Permission, Role, RolePermission
    from scripts.bootstrap_n8n_lab import provision_n8n_lab

    provision_n8n_lab(session, organization_id=1)
    session.commit()

    role = session.scalar(
        select(Role).where(
            Role.organization_id == 1,
            Role.code == "integration-sales-agent-v0",
        )
    )
    assert role is not None
    codes = set(
        session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role.id)
        )
    )
    assert codes == {
        "conversations.read",
        "services.read",
        "locations.read",
        "availability.read",
        "contact_appointments.read",
        "contact_appointments.book",
        "conversations.manage",
        "deliveries.create",
    }


def test_debounce_buffer_is_bounded_and_isolated_by_conversation_identity() -> None:
    from integrations.n8n.wf_01_sales_agent_v0 import (
        ConversationScopedDebounce,
        normalize_inbound,
    )

    start = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    buffer = ConversationScopedDebounce(window=timedelta(milliseconds=250), max_events=2)
    first = normalize_inbound(
        {
            "message_id": "synthetic-1",
            "contact_id": "contact-a",
            "phone_e164": "+51999000001",
            "text": "request cleaning",
            "occurred_at": start.isoformat(),
        }
    )
    second = normalize_inbound(
        {
            "message_id": "synthetic-2",
            "contact_id": "contact-a",
            "phone_e164": "+51999000001",
            "text": "at Lince",
            "occurred_at": (start + timedelta(milliseconds=10)).isoformat(),
        }
    )
    other = normalize_inbound(
        {
            "message_id": "synthetic-other",
            "contact_id": "contact-b",
            "phone_e164": "+51999000002",
            "text": "other conversation",
            "occurred_at": start.isoformat(),
        }
    )

    buffer.add(first, now=start)
    buffer.add(second, now=start + timedelta(milliseconds=10))
    buffer.add(other, now=start)

    assert buffer.flush(now=start + timedelta(milliseconds=100)) == ()
    flushed = buffer.flush(now=start + timedelta(seconds=1))
    assert [event.provider_message_id for event in flushed] == [
        "synthetic-1",
        "synthetic-2",
        "synthetic-other",
    ]
    assert buffer.flush(now=start + timedelta(seconds=2)) == ()


def test_normalizer_accepts_only_synthetic_data_and_drops_business_claims() -> None:
    from integrations.n8n.wf_01_sales_agent_v0 import (
        WorkflowContractError,
        normalize_inbound,
    )

    with pytest.raises(WorkflowContractError):
        normalize_inbound(
            {
                "provider": "whatsapp",
                "message_id": "live-1",
                "contact_id": "live-contact",
                "text": "live clinic message",
                "occurred_at": "2026-09-05T12:00:00Z",
            }
        )
    with pytest.raises(WorkflowContractError):
        normalize_inbound(
            {
                "schema_version": "2.0",
                "message_id": "synthetic-2",
                "contact_id": "contact-a",
                "text": "unsupported schema",
                "occurred_at": "2026-09-05T12:00:00Z",
            }
        )

    normalized = normalize_inbound(
        {
            "message_id": "synthetic-3",
            "contact_id": "contact-a",
            "text": "request cleaning",
            "occurred_at": "2026-09-05T12:00:00Z",
            "promotions": [{"code": "not-in-scope"}],
            "base_price": "not-in-scope",
        }
    )
    assert normalized.backend_payload()["provider"] == "test"
    assert "promotions" not in normalized.backend_payload()
    assert "base_price" not in normalized.backend_payload()


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None
    or importlib.util.find_spec("langgraph") is None,
    reason="WF-01 E2E runs in the optional sales-agent dependency job",
)
def test_wf01_three_turn_loop_persists_once_and_keeps_threads_isolated(
    migrated_engine, session, w4_agent_database_url
) -> None:
    from fastapi.testclient import TestClient

    from app import create_app
    from app.db import get_db
    from sales_agent.api import create_app as create_sales_agent_app
    from sales_agent.gateway import BackendGateway
    from sales_agent.memory import PostgresAgentMemory
    from sales_agent.runtime import SalesAgentRuntime

    from conftest import AUTH_HEADERS
    from integrations.n8n.wf_01_sales_agent_v0 import WF01Runner

    from scripts.bootstrap_n8n_lab import provision_n8n_lab

    config = provision_n8n_lab(session, organization_id=1)
    session.commit()

    maker = sessionmaker(
        bind=migrated_engine, autoflush=False, expire_on_commit=False
    )
    backend_app = create_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    backend_app.dependency_overrides[get_db] = _db
    backend_app.state.auth_sessionmaker = maker
    observed: dict[str, Any] = {}
    model = _scenario_model(observed)

    class RecordingClient:
        def __init__(self, client):
            self.client = client
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def post(self, url, **kwargs):
            self.calls.append((str(url), dict(kwargs.get("json", {}))))
            return self.client.post(url, **kwargs)

    with TestClient(backend_app, headers=AUTH_HEADERS) as raw_backend:
        recording_backend = RecordingClient(raw_backend)
        gateway = BackendGateway(
            "http://testserver",
            config["ODONTOFLOW_AGENT_TOKEN"],
            http_client=recording_backend,
        )
        with PostgresAgentMemory.open(w4_agent_database_url, setup=True) as memory:
            runtime = SalesAgentRuntime(
                gateway=gateway,
                model=model,
                checkpointer=memory.checkpointer,
            )
            sales_app = create_sales_agent_app(runtime=runtime)
            with TestClient(sales_app) as sales_client:

                def execute(event):
                    return WF01Runner(
                        backend_client=recording_backend,
                        sales_agent_client=sales_client,
                        inbound_token=config["ODONTOFLOW_INBOUND_TOKEN"],
                        agent_token=config["ODONTOFLOW_AGENT_TOKEN"],
                    ).execute(event)

                a1 = execute(
                    _synthetic_event("a-1", "contact-a", "request cleaning A")
                )
                a2 = execute(_synthetic_event("a-2", "contact-a", "Lince Monday A"))
                with migrated_engine.connect() as check:
                    assert check.execute(text("SELECT count(*) FROM appointments")).scalar_one() == 0
                a3 = execute(_synthetic_event("a-3", "contact-a", "Yes confirm A"))
                a4 = execute(_synthetic_event("a-4", "contact-a", "Yes confirm A"))
                duplicate = execute(
                    _synthetic_event("a-3", "contact-a", "Yes confirm A")
                )
                with migrated_engine.connect() as check:
                    assert check.execute(text("SELECT count(*) FROM appointments")).scalar_one() == 1

                b1 = execute(
                    _synthetic_event("b-1", "contact-b", "request cleaning B")
                )
                b2 = execute(_synthetic_event("b-2", "contact-b", "Lince Monday B"))
                b3 = execute(_synthetic_event("b-3", "contact-b", "Yes confirm B"))

            context = gateway.call_tool(
                "get_reception_context",
                conversation_id=a1.conversation_id,
                arguments={"as_of": "2026-09-05"},
            )
            state_a = runtime.agent.get_state(runtime.invoke_config(a1.conversation_id))
            state_b = runtime.agent.get_state(runtime.invoke_config(b1.conversation_id))

    assert a1 is not None and a1.agent_response["outcome"] == "continue"
    assert a2 is not None and a2.agent_response["outcome"] == "proposed"
    assert a3 is not None and a3.agent_response["outcome"] == "confirmed"
    assert a4 is not None and a4.agent_response["outcome"] == "continue"
    assert duplicate is not None and duplicate.duplicate is True
    assert duplicate.agent_response is None
    assert b1 is not None and b2 is not None and b3 is not None
    assert a1.conversation_id == a2.conversation_id == a3.conversation_id == a4.conversation_id
    assert b1.conversation_id == b2.conversation_id == b3.conversation_id
    assert a1.conversation_id != b1.conversation_id

    assert a3.outbound_receipt is not None
    assert a4.outbound_receipt is not None
    assert a3.test_provider_result == {
        "provider": "test",
        "status": "accepted",
        "delivery_mode": "synthetic_only",
        "outbound_id": a3.outbound_receipt["outbound_id"],
        "canonical_status": "pending",
        "test_result_id": f"test-provider-{a3.outbound_receipt['outbound_id']}",
    }
    assert context.status == "success"
    assert context.data is not None
    assert "promotions" not in context.data
    assert "base_price" not in json.dumps(context.data)

    tool_calls = [
        payload["tool_name"]
        for path, payload in recording_backend.calls
        if path == "/agent-tools/call"
    ]
    assert tool_calls
    assert set(tool_calls).issubset(
        {
            "get_reception_context",
            "list_services",
            "list_locations",
            "query_available_slots",
            "propose_appointment",
            "confirm_appointment",
            "request_human_handoff",
        }
    )
    assert not {"propose_cancellation", "propose_reschedule"}.intersection(tool_calls)

    assert len(state_a.values["messages"]) > 0
    assert len(state_b.values["messages"]) > 0
    human_a = [
        message.content for message in state_a.values["messages"] if message.type == "human"
    ]
    human_b = [
        message.content for message in state_b.values["messages"] if message.type == "human"
    ]
    assert human_a == [
        "request cleaning A",
        "Lince Monday A",
        "Yes confirm A",
        "Yes confirm A",
    ]
    assert human_b == ["request cleaning B", "Lince Monday B", "Yes confirm B"]

    appointment_starts = [
        row[0]
        for row in session.execute(
            text("SELECT start_utc FROM appointments ORDER BY id")
        ).all()
    ]
    observed_starts = [
        datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
        for slot in observed["slots"]
    ]
    assert appointment_starts == observed_starts

    assert session.execute(text("SELECT count(*) FROM messages WHERE direction='inbound'")).scalar_one() == 7
    assert session.execute(text("SELECT count(*) FROM messages WHERE direction='outbound'")).scalar_one() == 7
    assert session.execute(text("SELECT count(*) FROM appointments")).scalar_one() == 2
    assert session.execute(text("SELECT count(*) FROM appointment_proposals WHERE status='confirmed'")).scalar_one() == 2


def test_wf01_handoff_blocks_agent_tool_automation(
    migrated_engine, session
) -> None:
    from fastapi.testclient import TestClient

    from app import create_app
    from app.db import get_db
    from integrations.n8n.wf_01_sales_agent_v0 import normalize_inbound

    from scripts.bootstrap_n8n_lab import provision_n8n_lab

    config = provision_n8n_lab(session, organization_id=1)
    session.commit()
    maker = sessionmaker(
        bind=migrated_engine, autoflush=False, expire_on_commit=False
    )
    backend_app = create_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    backend_app.dependency_overrides[get_db] = _db
    backend_app.state.auth_sessionmaker = maker
    with TestClient(backend_app) as client:
        event = normalize_inbound(
            _synthetic_event("handoff-1", "contact-handoff", "human please")
        )
        inbound = client.post(
            "/internal/messages/inbound",
            headers={
                "Authorization": f"Bearer {config['ODONTOFLOW_INBOUND_TOKEN']}",
                "Idempotency-Key": event.inbound_idempotency_key,
                "X-Request-Id": event.request_id,
                "X-Correlation-Id": event.request_id,
            },
            json=event.backend_payload(),
        )
        assert inbound.status_code == 201, inbound.text
        conversation_id = inbound.json()["conversation_id"]

        def call(tool_name: str, arguments: dict[str, Any]):
            request_id = str(uuid4())
            correlation_id = str(uuid4())
            idempotency_key = str(uuid4())
            return client.post(
                "/agent-tools/call",
                headers={
                    "Authorization": f"Bearer {config['ODONTOFLOW_AGENT_TOKEN']}",
                    "X-Request-Id": request_id,
                    "X-Correlation-Id": correlation_id,
                    "Idempotency-Key": idempotency_key,
                },
                json={
                    "tool_version": "1.1",
                    "tool_name": tool_name,
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                    "arguments": arguments,
                },
            )

        handoff = call(
            "request_human_handoff",
            {"reason_code": "requested_by_contact", "reason_summary": "Contact requested a person."},
        )
        assert handoff.status_code == 200, handoff.text
        assert handoff.json()["status"] == "success"

        blocked_request_id = str(uuid4())
        blocked_correlation_id = str(uuid4())
        blocked = client.post(
            "/agent-tools/call",
            headers={
                "Authorization": f"Bearer {config['ODONTOFLOW_AGENT_TOKEN']}",
                "X-Request-Id": blocked_request_id,
                "X-Correlation-Id": blocked_correlation_id,
            },
            json={
                "tool_version": "1.0",
                "tool_name": "list_services",
                "conversation_id": conversation_id,
                "request_id": blocked_request_id,
                "correlation_id": blocked_correlation_id,
                "idempotency_key": None,
                "arguments": {},
            },
        )

    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "error"
    assert blocked.json()["error"]["code"] == "ENTITY_INACTIVE"
