"""B1 — ToolSpec registry, server allowlist, stable idempotency key, catalog."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import AUTH_HEADERS
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker
from test_reception_agent_phase5 import _seed_reception

from app import create_app
from app.db import get_db
from app.iam.permissions import PERMISSION_CODES
from app.idempotency.models import CommandReceipt
from app.scheduling.models import Appointment, AppointmentProposal
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

L4_TOOLS = {"confirm_appointment", "confirm_cancellation", "confirm_reschedule"}
PREVIOUS_READS = {
    "list_services",
    "list_locations",
    "list_eligible_practitioners",
    "query_available_slots",
    "get_appointment",
    "list_contact_appointments",
    "get_reception_context",
    "get_contact_profile",
}
PREVIOUS_MUTATIONS = {
    "propose_appointment",
    "confirm_appointment",
    "register_contact_profile",
    "propose_cancellation",
    "confirm_cancellation",
    "propose_reschedule",
    "confirm_reschedule",
    "request_human_handoff",
}


def _make_client(migrated_engine, *, headers=AUTH_HEADERS):
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
    return TestClient(app, raise_server_exceptions=False, headers=headers)


@pytest.fixture
def client(migrated_engine):
    return _make_client(migrated_engine)


def _agent(session, *, name: str, profile: str = "conversation-agent"):
    from app.iam.credentials import issue_credential
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session, organization_id=ORG, name=name, principal_type="agent"
    )
    _assign_profile(session, organization_id=ORG, principal_id=principal.id, profile=profile)
    _credential, token = issue_credential(
        session, organization_id=ORG, principal_id=principal.id, name=name
    )
    session.commit()
    return principal, {"Authorization": f"Bearer {token}"}


def _post(
    client,
    *,
    tool_name: str,
    arguments: dict,
    conversation_id: int | None,
    auth_headers: dict | None = None,
    key: str | None = None,
    envelope_request_id: str | None = None,
):
    from app.agent_tools.registry import READ_TOOL_NAMES

    mutation = tool_name not in READ_TOOL_NAMES
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    idem = (key or str(uuid4())) if mutation else None
    headers = {"X-Request-Id": request_id, "X-Correlation-Id": correlation_id}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    if auth_headers is not None:
        headers.update(auth_headers)
    body = {
        "tool_version": "1.1" if mutation else "1.0",
        "tool_name": tool_name,
        "request_id": envelope_request_id or request_id,
        "correlation_id": correlation_id,
        "idempotency_key": idem,
        "arguments": arguments,
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/agent-tools/call", headers=headers, json=body)
    return response, request_id, correlation_id


def _denials(session):
    return session.execute(
        text(
            "SELECT organization_id, principal_id, outcome, request_id, correlation_id, "
            "metadata FROM security_events WHERE event_type='agent_tool_denied' "
            "ORDER BY id"
        )
    ).all()


def _tool_audits(session):
    return session.execute(
        text(
            "SELECT entity_id, actor_id, request_id, correlation_id, after_state "
            "FROM audit_events WHERE action='agent_tool.called' ORDER BY id"
        )
    ).all()


# --- 1/2. The registry is the single, complete source ------------------------


def test_every_tool_spec_declares_level_effect_strict_args_and_real_permissions():
    from app.agent_tools.registry import TOOL_REGISTRY, ToolSpec
    from app.agent_tools.schemas import ToolName

    assert set(TOOL_REGISTRY) == set(ToolName.__args__)
    assert len(TOOL_REGISTRY) == 16
    for name, spec in TOOL_REGISTRY.items():
        assert isinstance(spec, ToolSpec)
        assert spec.name == name
        assert spec.level in {"L0", "L1", "L2", "L3", "L4"}, name
        assert spec.effect in {"read", "propose", "execute"}, name
        assert spec.args_model.model_config.get("extra") == "forbid", name
        assert spec.permissions, name
        assert set(spec.permissions) <= set(PERMISSION_CODES), name
        assert callable(spec.handler), name
        assert isinstance(spec.needs_conversation, bool), name
        assert spec.description.strip(), name
        assert (spec.effect == "read") == (spec.level == "L0"), name


def test_derived_names_match_previous_contract_and_reception_excludes_l4():
    from app.agent_tools.registry import (
        AGENT_DEFINITIONS,
        MUTATION_TOOL_NAMES,
        READ_TOOL_NAMES,
        TOOL_REGISTRY,
    )
    from app.agent_tools.service import ARGUMENT_MODELS

    assert set(READ_TOOL_NAMES) == PREVIOUS_READS
    assert set(MUTATION_TOOL_NAMES) == PREVIOUS_MUTATIONS
    assert set(ARGUMENT_MODELS) == PREVIOUS_READS | PREVIOUS_MUTATIONS
    assert {n for n, s in TOOL_REGISTRY.items() if s.level == "L4"} == L4_TOOLS
    assert AGENT_DEFINITIONS["reception"] == frozenset(TOOL_REGISTRY) - L4_TOOLS
    assert all(
        TOOL_REGISTRY[tool].level != "L4"
        for tools in AGENT_DEFINITIONS.values()
        for tool in tools
    )


def test_sales_agent_wrappers_are_a_non_l4_subset_consistent_with_registry():
    from app.agent_tools.registry import AGENT_DEFINITIONS, TOOL_REGISTRY
    from sales_agent import schemas as sales_schemas

    v0 = set(sales_schemas.V0_TOOL_NAMES)
    assert v0 <= AGENT_DEFINITIONS["reception"]
    assert all(TOOL_REGISTRY[tool].level != "L4" for tool in v0)
    assert set(sales_schemas.READ_TOOL_NAMES) == {
        tool for tool in v0 if TOOL_REGISTRY[tool].effect == "read"
    }
    assert set(sales_schemas.MUTATION_TOOL_NAMES) == {
        tool for tool in v0 if TOOL_REGISTRY[tool].effect != "read"
    }


# --- 3. L4 is never invocable by an agent -----------------------------------


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("confirm_appointment", {"proposal_id": 1, "confirmation_token": str(uuid4())}),
        (
            "confirm_cancellation",
            {"proposal_id": 1, "confirmation_token": str(uuid4()), "source_message_id": 1},
        ),
        ("confirm_reschedule", {"proposal_id": 1, "confirmation_token": str(uuid4())}),
    ],
)
def test_agent_calling_l4_gets_403_security_event_and_audit_row(
    client, session, tool_name, arguments
):
    seeded = _seed_reception(session, suffix=f"l4-{tool_name}", phone="+51999130001")
    principal, headers = _agent(session, name="b1-l4-agent")

    response, request_id, correlation_id = _post(
        client,
        tool_name=tool_name,
        arguments=arguments,
        conversation_id=seeded["conversation"].id,
        auth_headers=headers,
    )

    assert response.status_code == 403, response.text
    assert response.json() == {
        "error": {
            "code": "PERMISSION_DENIED",
            "message": "This tool is not available to the calling agent.",
            "details": {},
        }
    }
    session.expire_all()
    denials = _denials(session)
    assert len(denials) == 1
    org_id, principal_id, outcome, sec_request, sec_correlation, metadata = denials[0]
    assert (org_id, principal_id, outcome) == (ORG, principal.id, "blocked")
    assert (sec_request, sec_correlation) == (request_id, correlation_id)
    assert metadata == {"tool_name": tool_name, "agent_key": "reception", "level": "L4"}
    audits = _tool_audits(session)
    assert len(audits) == 1
    entity_id, actor_id, audit_request, audit_correlation, after_state = audits[0]
    assert entity_id == str(seeded["conversation"].id)
    assert actor_id == str(principal.id)
    assert (audit_request, audit_correlation) == (request_id, correlation_id)
    assert after_state["status"] == "error"
    assert after_state["error_code"] == "PERMISSION_DENIED"
    assert after_state["agent_key"] == "reception"
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    assert session.scalar(select(func.count()).select_from(CommandReceipt)) == 0


def test_l4_gate_runs_before_trace_validation(client, session):
    seeded = _seed_reception(session, suffix="l4-trace", phone="+51999130002")
    _principal, headers = _agent(session, name="b1-l4-trace-agent")

    response, _request_id, _correlation = _post(
        client,
        tool_name="confirm_appointment",
        arguments={"proposal_id": 1, "confirmation_token": str(uuid4())},
        conversation_id=seeded["conversation"].id,
        auth_headers=headers,
        envelope_request_id=str(uuid4()),  # mismatched on purpose
    )

    assert response.status_code == 403, response.text
    session.expire_all()
    assert len(_denials(session)) == 1


def test_l4_stays_denied_even_if_an_allowlist_lists_it(client, session, monkeypatch):
    from app.agent_tools import registry

    seeded = _seed_reception(session, suffix="l4-list", phone="+51999130003")
    _principal, headers = _agent(session, name="b1-l4-listed-agent")
    monkeypatch.setitem(registry.AGENT_KEY_BY_DISPLAY_NAME, "b1-l4-listed-agent", "b1-test")
    monkeypatch.setitem(
        registry.AGENT_DEFINITIONS, "b1-test", frozenset(registry.TOOL_REGISTRY)
    )

    response, _request_id, _correlation = _post(
        client,
        tool_name="confirm_appointment",
        arguments={"proposal_id": 1, "confirmation_token": str(uuid4())},
        conversation_id=seeded["conversation"].id,
        auth_headers=headers,
    )

    assert response.status_code == 403, response.text
    session.expire_all()
    denials = _denials(session)
    assert len(denials) == 1
    assert denials[0][5]["agent_key"] == "b1-test"


# --- 4. Allowlist per agent_key ---------------------------------------------


def test_tool_outside_the_agent_allowlist_is_denied_but_humans_keep_it(
    client, session, monkeypatch
):
    from app.agent_tools import registry

    seeded = _seed_reception(session, suffix="narrow", phone="+51999130004")
    principal, headers = _agent(session, name="b1-narrow-agent")
    monkeypatch.setitem(registry.AGENT_KEY_BY_DISPLAY_NAME, "b1-narrow-agent", "b1-narrow")
    monkeypatch.setitem(
        registry.AGENT_DEFINITIONS, "b1-narrow", frozenset({"list_services"})
    )

    allowed, _r, _c = _post(
        client,
        tool_name="list_services",
        arguments={},
        conversation_id=seeded["conversation"].id,
        auth_headers=headers,
    )
    denied, _r, _c = _post(
        client,
        tool_name="get_reception_context",
        arguments={},
        conversation_id=seeded["conversation"].id,
        auth_headers=headers,
    )
    human, _r, _c = _post(
        client,
        tool_name="get_reception_context",
        arguments={},
        conversation_id=seeded["conversation"].id,
    )

    assert allowed.status_code == 200 and allowed.json()["status"] == "success", allowed.text
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert "list_services" not in denied.text
    assert human.status_code == 200 and human.json()["status"] == "success", human.text
    session.expire_all()
    denials = _denials(session)
    assert len(denials) == 1
    assert denials[0][1] == principal.id
    assert denials[0][5] == {
        "tool_name": "get_reception_context",
        "agent_key": "b1-narrow",
        "level": "L0",
    }
    agent_keys = [row[4]["agent_key"] for row in _tool_audits(session)]
    assert agent_keys == ["b1-narrow", "b1-narrow", None]


def test_agent_propose_keeps_receipt_claim_as_first_statement(
    client, session, migrated_engine
):
    seeded = _seed_reception(session, suffix="claim-first", phone="+51999130005")
    _principal, headers = _agent(session, name="b1-claim-first-agent")
    log: list[tuple[str, int, str]] = []

    def on_begin(conn):
        log.append(("begin", id(conn), ""))

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        log.append(("sql", id(conn), statement))

    event.listen(migrated_engine, "begin", on_begin)
    event.listen(migrated_engine, "before_cursor_execute", on_execute)
    try:
        response, _r, _c = _post(
            client,
            tool_name="propose_appointment",
            arguments={
                "full_name": "Paciente B1",
                "service_id": seeded["service"].id,
                "location_id": seeded["location"].id,
                "practitioner_id": seeded["practitioner"].id,
                "start": "2026-08-24T09:00:00-05:00",
            },
            conversation_id=seeded["conversation"].id,
            auth_headers=headers,
        )
    finally:
        event.remove(migrated_engine, "begin", on_begin)
        event.remove(migrated_engine, "before_cursor_execute", on_execute)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success", response.text
    claim_index = next(
        index
        for index, (kind, _conn, statement) in enumerate(log)
        if kind == "sql" and statement.lstrip().upper().startswith("INSERT INTO COMMAND_RECEIPTS")
    )
    claim_conn = log[claim_index][1]
    previous = [row for row in log[:claim_index] if row[1] == claim_conn]
    assert previous and previous[-1][0] == "begin", previous[-3:]
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AppointmentProposal)) == 1


# --- 5. conversation_id is optional; needs_conversation enforces it -----------


def test_missing_conversation_for_conversation_tool_is_invalid_input(client, session):
    response, _r, _c = _post(
        client, tool_name="list_services", arguments={}, conversation_id=None
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "INVALID_INPUT"
    session.expire_all()
    audits = _tool_audits(session)
    assert len(audits) == 1
    assert audits[0][0] == "none"
    assert audits[0][4]["agent_key"] is None


# --- 6. Catalog filtered by caller ------------------------------------------


def test_catalog_is_filtered_by_calling_principal(client, session, migrated_engine):
    _principal, headers = _agent(session, name="b1-catalog-agent")

    human = client.get("/agent-tools/catalog")
    agent = client.get("/agent-tools/catalog", headers=headers)
    anonymous = _make_client(migrated_engine, headers={}).get("/agent-tools/catalog")

    assert human.status_code == 200, human.text
    assert agent.status_code == 200, agent.text
    assert anonymous.status_code == 401, anonymous.text
    human_tools = {item["name"]: item for item in human.json()["tools"]}
    agent_tools = {item["name"]: item for item in agent.json()["tools"]}
    assert len(human_tools) == 16
    assert len(agent_tools) == 13
    assert not L4_TOOLS & set(agent_tools)
    assert all(item["level"] != "L4" for item in agent_tools.values())
    propose = human_tools["propose_appointment"]
    assert propose["effect"] == "propose"
    assert propose["level"] == "L3"
    assert propose["tool_version"] == "1.1"
    assert propose["needs_conversation"] is True
    assert "start" in propose["arguments_schema"]["properties"]
    assert human_tools["list_services"]["tool_version"] == "1.0"
    assert all(item["description"] for item in human_tools.values())


# --- 7/8. Gateway: stable key and OUTCOME_UNKNOWN ----------------------------


def _gateway(handler):
    from sales_agent.gateway import BackendGateway

    return BackendGateway(
        "http://backend.test",
        "synthetic-credential",
        http_client=httpx.Client(
            base_url="http://backend.test", transport=httpx.MockTransport(handler)
        ),
    )


def _success(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "tool_version": "1.1",
            "status": "success",
            "data": {},
            "error": None,
            "request_id": request.headers["X-Request-Id"],
            "correlation_id": request.headers["X-Correlation-Id"],
            "duration_ms": 1,
        },
    )


PROPOSE_ARGS = {
    "full_name": "Paciente B1",
    "service_id": 1,
    "location_id": 2,
    "practitioner_id": 3,
    "start": datetime(2026, 8, 24, 9, tzinfo=timezone(timedelta(hours=-5))),
}


def test_gateway_idempotency_key_is_stable_per_logical_action():
    keys: list[str] = []

    def handler(request):
        keys.append(request.headers["Idempotency-Key"])
        return _success(request)

    gateway = _gateway(handler)

    def call(*, latest=40, arguments=PROPOSE_ARGS, tool="propose_appointment"):
        gateway.call_tool(
            tool,
            conversation_id=17,
            arguments=dict(arguments),
            latest_inbound_message_id=latest,
        )
        return keys[-1]

    first = call()
    same = call()
    same_instant_utc = call(
        arguments={**PROPOSE_ARGS, "start": datetime(2026, 8, 24, 14, tzinfo=timezone.utc)}
    )
    other_inbound = call(latest=41)
    other_args = call(arguments={**PROPOSE_ARGS, "practitioner_id": 4})
    other_tool = call(
        tool="request_human_handoff",
        arguments={"reason_code": "other", "reason_summary": "Synthetic reason"},
    )

    assert UUID(first).version == 4 and str(UUID(first)) == first
    assert first == same == same_instant_utc
    assert len({first, other_inbound, other_args, other_tool}) == 4
    # No turn identity: behaves as before (fresh key per call).
    gateway.call_tool("propose_appointment", conversation_id=17, arguments=dict(PROPOSE_ARGS))
    gateway.call_tool("propose_appointment", conversation_id=17, arguments=dict(PROPOSE_ARGS))
    assert keys[-1] != keys[-2]


def test_gateway_anchors_the_turn_on_the_loaded_inbound_message():
    """The runtime never threads the inbound id through tool wrappers: loading
    the turn's inbound message anchors the stable key for that conversation."""
    import json

    from sales_agent.gateway import stable_idempotency_key

    keys: list[str | None] = []

    def handler(request):
        body = json.loads(request.content)
        keys.append(request.headers.get("Idempotency-Key"))
        response = _success(request)
        if body["tool_name"] == "get_reception_context":
            payload = response.json()
            payload["data"] = {
                "conversation": {"id": body["conversation_id"]},
                "recent_messages": [
                    {
                        "id": 40,
                        "direction": "inbound",
                        "text": "Synthetic inbound",
                        "occurred_at": "2026-08-24T12:00:00Z",
                    }
                ],
            }
            return httpx.Response(200, json=payload)
        return response

    gateway = _gateway(handler)
    gateway.load_latest_inbound_message(17, 40)
    gateway.call_tool("propose_appointment", conversation_id=17, arguments=dict(PROPOSE_ARGS))
    gateway.call_tool("propose_appointment", conversation_id=17, arguments=dict(PROPOSE_ARGS))
    expected = str(
        stable_idempotency_key(
            conversation_id=17,
            latest_inbound_message_id=40,
            tool_name="propose_appointment",
            arguments=dict(PROPOSE_ARGS),
        )
    )
    assert keys[-2:] == [expected, expected]
    # Another conversation has no anchor yet: fresh key per call.
    gateway.call_tool("propose_appointment", conversation_id=18, arguments=dict(PROPOSE_ARGS))
    gateway.call_tool("propose_appointment", conversation_id=18, arguments=dict(PROPOSE_ARGS))
    assert keys[-1] != keys[-2] and expected not in keys[-2:]


def test_gateway_maps_mutation_timeouts_to_outcome_unknown():
    from sales_agent.schemas import GatewayError

    def raising(exc_type):
        def handler(request):
            raise exc_type("synthetic", request=request)

        return handler

    with pytest.raises(GatewayError) as unknown:
        _gateway(raising(httpx.ReadTimeout)).call_tool(
            "propose_appointment",
            conversation_id=17,
            arguments=dict(PROPOSE_ARGS),
            latest_inbound_message_id=40,
        )
    assert unknown.value.code == "OUTCOME_UNKNOWN"
    assert unknown.value.status_code == 504

    with pytest.raises(GatewayError) as read_timeout:
        _gateway(raising(httpx.ReadTimeout)).call_tool(
            "list_services", conversation_id=17, arguments={}
        )
    assert read_timeout.value.code == "BACKEND_UNAVAILABLE"

    for exc_type in (httpx.ConnectError, httpx.ConnectTimeout):
        with pytest.raises(GatewayError) as not_sent:
            _gateway(raising(exc_type)).call_tool(
                "propose_appointment",
                conversation_id=17,
                arguments=dict(PROPOSE_ARGS),
                latest_inbound_message_id=40,
            )
        assert not_sent.value.code == "BACKEND_UNAVAILABLE"

    for status in (502, 504):
        with pytest.raises(GatewayError) as proxy:
            _gateway(lambda request, status=status: httpx.Response(status, text="gw")).call_tool(
                "propose_appointment",
                conversation_id=17,
                arguments=dict(PROPOSE_ARGS),
                latest_inbound_message_id=40,
            )
        assert proxy.value.code == "OUTCOME_UNKNOWN"


# --- Runtime: an unknown mutation outcome never becomes 'proposed' -----------


def _propose_then_claim_model():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    return FakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "propose_appointment",
                            "args": {
                                **PROPOSE_ARGS,
                                "start": PROPOSE_ARGS["start"].isoformat(),
                            },
                            "id": "propose-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "SalesAgentResponse",
                            "args": {
                                "reply": "Tu cita quedó propuesta.",
                                "outcome": "proposed",
                                "handoff": False,
                            },
                            "id": "structured-1",
                        }
                    ],
                ),
            ]
        )
    )


def _runtime_gateway(*, handoff_ok: bool):
    seen: list[tuple[str, str | None]] = []

    def handler(request):
        import json

        body = json.loads(request.content)
        seen.append((body["tool_name"], request.headers.get("Idempotency-Key")))
        if body["tool_name"] == "propose_appointment":
            raise httpx.ReadTimeout("synthetic", request=request)
        if body["tool_name"] == "request_human_handoff" and handoff_ok:
            return _success(request)
        raise httpx.ConnectError("synthetic", request=request)

    return _gateway(handler), seen


@pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="runtime tests run in the optional sales-agent dependency job",
)
@pytest.mark.parametrize("handoff_ok", [True, False])
def test_runtime_outcome_unknown_hands_off_or_errors_never_proposed(handoff_ok):
    from sales_agent.runtime import SalesAgentRuntime
    from sales_agent.schemas import GatewayError, SalesAgentTurnRequest

    gateway, seen = _runtime_gateway(handoff_ok=handoff_ok)
    gateway.load_latest_inbound_message = lambda conversation_id, message_id: {
        "id": message_id,
        "text": "Quiero la cita de las 9",
    }
    runtime = SalesAgentRuntime(
        gateway=gateway,
        model=_propose_then_claim_model(),
        checkpointer=None,
        telemetry_sink=lambda _event: None,
    )
    request = SalesAgentTurnRequest(conversation_id=17, latest_inbound_message_id=40)

    if handoff_ok:
        response = runtime.turn(request)
        assert response.outcome == "handoff"
        assert response.handoff is True
    else:
        with pytest.raises(GatewayError) as failure:
            runtime.turn(request)
        assert failure.value.code == "OUTCOME_UNKNOWN"
    assert [tool for tool, _key in seen] == ["propose_appointment", "request_human_handoff"]
