"""W3 Sales Agent runtime contract tests.

These tests intentionally import the optional sales-agent surface directly so
the first RED run fails for the missing implementation, while the base app
remains testable without LangChain installed.
"""

from __future__ import annotations

import ast
import json
import importlib.util
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from itertools import repeat
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
V0_TOOL_NAMES = {
    "get_reception_context",
    "list_services",
    "list_locations",
    "query_available_slots",
    "propose_appointment",
    "confirm_appointment",
    "request_human_handoff",
}


def test_app_source_has_no_langchain_or_langgraph_imports() -> None:
    forbidden = {"langchain", "langgraph"}
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".", 1)[0] for alias in node.names}
                assert imported.isdisjoint(forbidden), path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".", 1)[0] not in forbidden, path


def test_sales_agent_package_is_optional_and_not_imported_by_app() -> None:
    assert importlib.util.find_spec("app") is not None
    assert importlib.util.find_spec("sales_agent") is not None


def test_langchain_dependencies_are_optional_and_app_import_isolated() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base_dependencies = " ".join(project["project"]["dependencies"]).lower()
    assert "langchain" not in base_dependencies
    assert "langgraph" not in base_dependencies
    assert {"agent"}.issubset(project["project"]["optional-dependencies"])

    script = """
import importlib.abc
import sys

class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'langchain' or fullname.startswith('langchain.'):
            raise ModuleNotFoundError('blocked optional dependency')
        if fullname == 'langgraph' or fullname.startswith('langgraph.'):
            raise ModuleNotFoundError('blocked optional dependency')
        return None

sys.meta_path.insert(0, BlockOptional())
from app import create_app
from sales_agent.api import create_app as create_sales_agent_app
assert create_app is not None
assert create_sales_agent_app is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_only_the_seven_v0_tools_are_exposed() -> None:
    from sales_agent.gateway import BackendGateway
    from sales_agent.tools import build_v0_tools

    gateway = BackendGateway("http://backend.test", "ofk_test")
    tools = build_v0_tools(gateway, conversation_id=17)

    assert {item.name for item in tools} == V0_TOOL_NAMES
    assert len(tools) == 7
    assert not {"get_contact_profile", "propose_cancellation", "propose_reschedule"}.intersection(
        item.name for item in tools
    )
    assert all("Args:" in (item.description or "") for item in tools)


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_turn_contract_uses_structured_response_and_thread_id() -> None:
    from sales_agent.runtime import SalesAgentRuntime
    from sales_agent.schemas import SalesAgentTurnRequest

    assert SalesAgentTurnRequest.model_validate(
        {"conversation_id": 17, "latest_inbound_message_id": 23}
    ).conversation_id == 17
    assert "thread_id" in SalesAgentRuntime.invoke_config_fields()
    assert SalesAgentRuntime.structured_response_fields() == {
        "reply",
        "outcome",
        "handoff",
    }


@pytest.fixture
def gateway_transport():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tool_version": payload["tool_version"],
                "status": "success",
                "data": {"services": []},
                "error": None,
                "request_id": str(payload["request_id"]),
                "correlation_id": str(payload["correlation_id"]),
                "duration_ms": 1,
            },
        )

    return httpx.MockTransport(handler), calls


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_tool_wrapper_calls_authenticated_typed_gateway(gateway_transport) -> None:
    from sales_agent.gateway import BackendGateway
    from sales_agent.tools import build_v0_tools

    transport, calls = gateway_transport
    with httpx.Client(transport=transport, base_url="http://backend.test") as client:
        gateway = BackendGateway(
            "http://backend.test",
            "ofk_test",
            http_client=client,
        )
        tools = build_v0_tools(gateway, conversation_id=17)
        result = tools[1].invoke({})

    assert result["status"] == "success"
    assert len(calls) == 1
    request = calls[0]
    payload = json.loads(request.content)
    assert request.url.path == "/agent-tools/call"
    assert request.headers["authorization"] == "Bearer ofk_test"
    assert payload["tool_name"] == "list_services"
    assert payload["conversation_id"] == 17
    assert payload["idempotency_key"] is None
    UUID(payload["request_id"])
    UUID(payload["correlation_id"])
    assert request.headers["x-request-id"] == payload["request_id"]
    assert request.headers["x-correlation-id"] == payload["correlation_id"]


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_latest_inbound_is_loaded_through_context_gateway(gateway_transport) -> None:
    from sales_agent.gateway import BackendGateway

    transport, calls = gateway_transport

    def context_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tool_version": "1.0",
                "status": "success",
                "data": {
                    "conversation": {"id": payload["conversation_id"]},
                    "recent_messages": [
                        {
                            "id": 23,
                            "direction": "inbound",
                            "text": "Synthetic latest inbound",
                            "occurred_at": "2026-09-05T12:00:00Z",
                        }
                    ],
                },
                "error": None,
                "request_id": str(payload["request_id"]),
                "correlation_id": str(payload["correlation_id"]),
                "duration_ms": 1,
            },
        )

    with httpx.Client(
        transport=httpx.MockTransport(context_handler),
        base_url="http://backend.test",
    ) as client:
        gateway = BackendGateway("http://backend.test", "ofk_test", http_client=client)
        inbound = gateway.load_latest_inbound_message(17, 23)

    assert inbound.id == 23
    assert inbound.text == "Synthetic latest inbound"
    assert len(calls) == 0


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_gateway_preserves_typed_domain_error_and_normalizes_http_error() -> None:
    from sales_agent.gateway import BackendGateway
    from sales_agent.schemas import AgentToolError, GatewayError

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["tool_name"] == "list_services":
            return httpx.Response(
                200,
                json={
                    "tool_version": "1.0",
                    "status": "error",
                    "data": None,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": "The tool arguments are invalid.",
                        "retryable": False,
                        "details": {},
                    },
                    "request_id": str(payload["request_id"]),
                    "correlation_id": str(payload["correlation_id"]),
                    "duration_ms": 1,
                },
            )
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": "The authenticated principal lacks permission.",
                    "details": {},
                }
            },
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://backend.test",
    ) as client:
        gateway = BackendGateway("http://backend.test", "ofk_test", http_client=client)
        domain_result = gateway.call_tool(
            "list_services", conversation_id=17, arguments={}
        )
        assert domain_result.status == "error"
        assert domain_result.error is not None
        with pytest.raises(GatewayError) as raised:
            gateway.call_tool("list_locations", conversation_id=17, arguments={})

    assert raised.value.code == "PERMISSION_DENIED"
    assert raised.value.status_code == 403


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_mutating_tool_uses_uuid4_idempotency_and_forbidden_tools_are_rejected(
    gateway_transport,
) -> None:
    from sales_agent.gateway import BackendGateway
    from sales_agent.tools import build_v0_tools

    transport, calls = gateway_transport
    with httpx.Client(transport=transport, base_url="http://backend.test") as client:
        gateway = BackendGateway("http://backend.test", "ofk_test", http_client=client)
        tools = build_v0_tools(gateway, conversation_id=17)
        result = tools[4].invoke(
            {
                "full_name": "Synthetic Patient",
                "service_id": 1,
                "location_id": 2,
                "practitioner_id": 3,
                "start": "2026-09-07T14:00:00Z",
            }
        )

        with pytest.raises(ValueError):
            gateway.call_tool("propose_cancellation", conversation_id=17, arguments={})

    assert result["status"] == "success"
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload["tool_name"] == "propose_appointment"
    parsed = UUID(payload["idempotency_key"])
    assert parsed.version == 4
    assert calls[0].headers["idempotency-key"] == str(parsed)


@pytest.fixture(scope="session")
def agent_database_url():
    from conftest import TEST_DATABASE_URL

    database_name = f"odontoflow_agent_w3_{os.getpid()}"
    server_url = make_url(TEST_DATABASE_URL).set(database="odontoflow")
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server_engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": database_name},
        ).scalar()
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    server_engine.dispose()
    yield make_url(TEST_DATABASE_URL).set(database=database_name).render_as_string(
        hide_password=False
    )
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    server_engine.dispose()


@pytest.fixture
def fake_gateway():
    from sales_agent.gateway import AgentToolResult

    calls: list[tuple[str, int, dict]] = []

    class FakeGateway:
        def load_latest_inbound_message(self, conversation_id: int, message_id: int):
            calls.append(("load_latest_inbound_message", conversation_id, {"id": message_id}))
            return {"id": message_id, "text": f"Synthetic inbound {conversation_id}"}

        def call_tool(self, tool_name: str, *, conversation_id: int, arguments: dict):
            calls.append((tool_name, conversation_id, arguments))
            if tool_name == "get_reception_context":
                return AgentToolResult(
                    tool_version="1.0",
                    status="success",
                    data={
                        "conversation": {"id": conversation_id},
                        "recent_messages": [
                            {
                                "id": 23,
                                "direction": "inbound",
                                "text": f"Synthetic inbound {conversation_id}",
                            }
                        ],
                    },
                    error=None,
                    request_id="00000000-0000-4000-8000-000000000001",
                    correlation_id="00000000-0000-4000-8000-000000000002",
                    duration_ms=1,
                )
            return AgentToolResult(
                tool_version="1.1" if tool_name in {"request_human_handoff"} else "1.0",
                status="success",
                data={},
                error=None,
                request_id="00000000-0000-4000-8000-000000000001",
                correlation_id="00000000-0000-4000-8000-000000000002",
                duration_ms=1,
            )

    return FakeGateway(), calls


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_same_thread_resumes_and_different_threads_are_isolated(
    agent_database_url, fake_gateway
) -> None:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from sales_agent.memory import PostgresAgentMemory
    from sales_agent.runtime import SalesAgentRuntime
    from sales_agent.schemas import SalesAgentTurnRequest

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = FakeModel(
        messages=repeat(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "SalesAgentResponse",
                        "args": {
                            "reply": "Synthetic response",
                            "outcome": "continue",
                            "handoff": False,
                        },
                        "id": "structured-1",
                    }
                ],
            )
        )
    )
    gateway, calls = fake_gateway
    with PostgresAgentMemory.open(agent_database_url, setup=True) as memory:
        runtime = SalesAgentRuntime(
            gateway=gateway,
            model=model,
            checkpointer=memory.checkpointer,
        )
        request = SalesAgentTurnRequest(
            conversation_id=101,
            latest_inbound_message_id=23,
        )
        first = runtime.turn(request)
        second = runtime.turn(request)
        other = runtime.turn(
            SalesAgentTurnRequest(conversation_id=202, latest_inbound_message_id=23)
        )

        state = runtime.agent.get_state(runtime.invoke_config(101))
        other_state = runtime.agent.get_state(runtime.invoke_config(202))

    assert first.reply == second.reply == other.reply == "Synthetic response"
    state_contents = [message.content for message in state.values["messages"]]
    other_contents = [message.content for message in other_state.values["messages"]]
    assert state_contents.count("Synthetic inbound 101") == 2
    assert "Synthetic inbound 202" not in state_contents
    assert other_contents.count("Synthetic inbound 202") == 1
    assert "Synthetic inbound 101" not in other_contents
    assert state.config["configurable"]["thread_id"] == "101"
    assert other_state.config["configurable"]["thread_id"] == "202"
    assert [row[1] for row in calls if row[0] == "load_latest_inbound_message"] == [101, 101, 202]


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_recursion_bound_calls_typed_handoff_and_emits_content_free_telemetry(
    fake_gateway,
) -> None:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from sales_agent.runtime import SalesAgentRuntime
    from sales_agent.schemas import SalesAgentTurnRequest

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    model = FakeModel(
        messages=repeat(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_services",
                        "args": {},
                        "id": "tool-1",
                    }
                ],
            )
        )
    )
    gateway, calls = fake_gateway
    telemetry: list[dict] = []
    runtime = SalesAgentRuntime(
        gateway=gateway,
        model=model,
        checkpointer=None,
        recursion_limit=2,
        telemetry_sink=telemetry.append,
    )

    response = runtime.turn(
        SalesAgentTurnRequest(conversation_id=404, latest_inbound_message_id=23)
    )

    assert response.handoff is True
    assert response.outcome == "handoff"
    assert any(row[0] == "request_human_handoff" for row in calls)
    assert len(telemetry) == 1
    assert set(telemetry[0]) == {
        "conversation_id",
        "model",
        "input_tokens",
        "output_tokens",
        "model_calls",
        "tool_calls",
        "tool_failures",
        "latency_ms",
        "outcome",
    }
    encoded = json.dumps(telemetry[0])
    assert "Synthetic inbound message" not in encoded
    assert "tool-1" not in encoded


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_tool_gateway_failure_is_counted_without_exposing_tool_details() -> None:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from sales_agent.gateway import AgentToolResult
    from sales_agent.schemas import AgentToolError, SalesAgentTurnRequest
    from sales_agent.runtime import SalesAgentRuntime

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    class FailingGateway:
        def load_latest_inbound_message(self, conversation_id, message_id):
            return {"id": message_id, "text": "Synthetic inbound"}

        def call_tool(self, tool_name, *, conversation_id, arguments):
            if tool_name == "list_services":
                return AgentToolResult(
                    tool_version="1.0",
                    status="error",
                    data=None,
                    error=AgentToolError(
                        code="BACKEND_UNAVAILABLE",
                        message="backend unavailable",
                        retryable=False,
                        details={"secret": "do-not-log"},
                    ),
                    request_id="00000000-0000-4000-8000-000000000001",
                    correlation_id="00000000-0000-4000-8000-000000000002",
                    duration_ms=1,
                )
            return AgentToolResult(
                tool_version="1.1",
                status="success",
                data={},
                error=None,
                request_id="00000000-0000-4000-8000-000000000001",
                correlation_id="00000000-0000-4000-8000-000000000002",
                duration_ms=1,
            )

    telemetry: list[dict] = []
    runtime = SalesAgentRuntime(
        gateway=FailingGateway(),
        model=FakeModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "list_services", "args": {}, "id": "secret-tool-id"}
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "SalesAgentResponse",
                                "args": {
                                    "reply": "I need to connect you with reception.",
                                    "outcome": "handoff",
                                    "handoff": True,
                                },
                                "id": "structured-error",
                            }
                        ],
                    ),
                ]
            )
        ),
        checkpointer=None,
        telemetry_sink=telemetry.append,
    )

    response = runtime.turn(
        SalesAgentTurnRequest(conversation_id=505, latest_inbound_message_id=23)
    )

    assert response.handoff is True
    assert telemetry[0]["tool_calls"] == 1
    assert telemetry[0]["tool_failures"] == 1
    assert telemetry[0]["model_calls"] == 2
    assert "secret-tool-id" not in json.dumps(telemetry[0])


def test_sales_agent_turn_api_has_typed_input_and_output_without_optional_imports() -> None:
    from fastapi.testclient import TestClient

    from sales_agent.api import create_app
    from sales_agent.schemas import SalesAgentTurnResponse

    class StubRuntime:
        def turn(self, request):
            return SalesAgentTurnResponse(
                conversation_id=request.conversation_id,
                latest_inbound_message_id=request.latest_inbound_message_id,
                reply="Synthetic reply",
                outcome="continue",
                handoff=False,
            )

    with TestClient(create_app(runtime=StubRuntime())) as client:
        response = client.post(
            "/sales-agent/turn",
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


def test_agent_database_url_cannot_be_canonical() -> None:
    from sales_agent.config import validate_agent_database_url

    with pytest.raises(ValueError):
        validate_agent_database_url(
            "postgresql://odontoflow:odontoflow@127.0.0.1:5434/odontoflow"
        )
    assert validate_agent_database_url(
        "postgresql://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent"
    ).database == "odontoflow_agent"


def test_runtime_rejects_an_unbounded_or_zero_recursion_limit() -> None:
    from sales_agent.runtime import SalesAgentRuntime

    with pytest.raises(ValueError):
        SalesAgentRuntime(gateway=object(), checkpointer=None, recursion_limit=0)
    with pytest.raises(ValueError):
        SalesAgentRuntime(gateway=object(), checkpointer=None, recursion_limit=101)


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="W3 runtime tests run in the optional sales-agent dependency job",
)
def test_dropping_agent_memory_database_does_not_touch_canonical_state(migrated_engine):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.audit.models import AuditEvent
    from app.messaging.models import Message
    from sales_agent.memory import PostgresAgentMemory
    from sales_agent.runtime import SalesAgentRuntime
    from sales_agent.schemas import SalesAgentTurnRequest

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    from conftest import TEST_DATABASE_URL

    from test_agent_tools_phase3 import _seed_conversation

    canonical_session = sessionmaker(bind=migrated_engine, expire_on_commit=False)()
    seeded = _seed_conversation(
        canonical_session,
        suffix="memory-drop",
        phone="+51999123456",
    )
    conversation = seeded["conversation"]
    canonical_session.add(
        Message(
            organization_id=conversation.organization_id,
            channel_account_id=conversation.channel_account_id,
            conversation_id=conversation.id,
            direction="inbound",
            provider_message_id="synthetic-memory-drop-message",
            message_type="text",
            body_text="Synthetic canonical message",
            delivery_status="received",
            occurred_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
            content_expires_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
        )
    )
    canonical_session.add(
        AuditEvent(
            organization_id=conversation.organization_id,
            actor_id="synthetic-w3",
            actor_type="test",
            action="sales_agent.test",
            entity_id=str(conversation.id),
            entity_type="conversation",
            after_state={"synthetic": True},
        )
    )
    canonical_session.commit()
    canonical_session.close()

    database_name = f"odontoflow_agent_drop_{os.getpid()}"
    server_url = make_url(TEST_DATABASE_URL).set(database="odontoflow")
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    server_engine.dispose()
    agent_url = make_url(TEST_DATABASE_URL).set(database=database_name).render_as_string(
        hide_password=False
    )
    with migrated_engine.connect() as connection:
        canonical_before = {
            table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("organizations", "conversations", "messages", "appointments", "audit_events")
        }
    model = FakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "SalesAgentResponse",
                            "args": {
                                "reply": "Synthetic response",
                                "outcome": "continue",
                                "handoff": False,
                            },
                            "id": "structured-drop",
                        }
                    ],
                )
            ]
        )
    )
    from sales_agent.gateway import AgentToolResult

    class Gateway:
        def load_latest_inbound_message(self, conversation_id, message_id):
            return {"id": message_id, "text": "Synthetic inbound"}

        def call_tool(self, tool_name, *, conversation_id, arguments):
            return AgentToolResult(
                tool_version="1.0",
                status="success",
                data={
                    "conversation": {"id": conversation_id},
                    "recent_messages": [],
                },
                error=None,
                request_id="00000000-0000-4000-8000-000000000001",
                correlation_id="00000000-0000-4000-8000-000000000002",
                duration_ms=1,
            )

    try:
        with PostgresAgentMemory.open(agent_url, setup=True) as memory:
            runtime = SalesAgentRuntime(
                gateway=Gateway(),
                model=model,
                checkpointer=memory.checkpointer,
            )
            runtime.turn(
                SalesAgentTurnRequest(
                    conversation_id=909,
                    latest_inbound_message_id=23,
                )
            )
        server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
        with server_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        server_engine.dispose()
        with migrated_engine.connect() as connection:
            canonical_after = {
                table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("organizations", "conversations", "messages", "appointments", "audit_events")
            }
    finally:
        server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
        with server_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        server_engine.dispose()

    assert all(value > 0 for value in canonical_before.values())
    assert canonical_after == canonical_before
