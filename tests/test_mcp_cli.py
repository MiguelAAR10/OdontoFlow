"""MCPCLI — the `odontoflow` CLI and the MCP server over the HTTP catalog.

Spec: ``docs/superpowers/specs/2026-10-01-erp-mcpcli.md``. Real PostgreSQL, one
pytest process. Both packages talk HTTP only; ``TestClient(app)`` is injected as
the ``httpx.Client`` so the real routes, auth and DB invariants answer.
"""

from __future__ import annotations

import ast
import asyncio
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import _count, _credential, _lucia
from test_collections_sweep import _airy_caller, _overdue
from test_reception_agent_phase5 import _seed_reception

from app import create_app
from app.db import get_db
from app.messaging.models import OutboundMessage
from app.proposals.models import AgentProposal

REPO_ROOT = Path(__file__).resolve().parents[1]
L4_TOOLS = {"confirm_appointment", "confirm_cancellation", "confirm_reschedule"}
FIXED_MCP_TOOLS = {"odontoflow_inbox", "odontoflow_start_cobranza_run", "odontoflow_me"}


# --- fixtures & helpers -------------------------------------------------------


def _app(migrated_engine):
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    app = create_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return app


@pytest.fixture
def client(migrated_engine):
    return TestClient(_app(migrated_engine), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _cobranza_enabled(monkeypatch):
    monkeypatch.delenv("AGENT_COBRANZA_ENABLED", raising=False)
    monkeypatch.delenv("ODONTOFLOW_TOKEN", raising=False)
    monkeypatch.delenv("ODONTOFLOW_URL", raising=False)


def _token(headers: dict) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def _reception(session, name="n8n-lab-agent"):
    _pid, headers = _credential(
        session, name=name, principal_type="agent", profile="conversation-agent"
    )
    return headers


def _seeded_conversation(session) -> int:
    seeded = _seed_reception(session, suffix=f"mcp-{uuid4().hex[:6]}", phone="+51987654321")
    return seeded["conversation"].id


class Recorder:
    """Capture every request the client sends (httpx event hook)."""

    def __init__(self, client):
        self.requests = []
        client.event_hooks = {"request": [self.requests.append], "response": []}

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def _cli(argv, *, token, client, monkeypatch, capsys):
    from odontoflow_cli.main import main

    if token is not None:
        monkeypatch.setenv("ODONTOFLOW_TOKEN", token)
    code = main(argv, http_client=client)
    out, err = capsys.readouterr()
    return code, out, err


def _catalog_names(client, headers) -> set[str]:
    response = client.get("/agent-tools/catalog", headers=headers)
    assert response.status_code == 200, response.text
    return {t["name"] for t in response.json()["tools"]}


# --- 1. tools list --------------------------------------------------------------


def test_tools_list_matches_the_server_catalog_for_the_token_without_l4(
    client, session, monkeypatch, capsys
):
    agent = _reception(session)
    _lucia_id, lucia = _lucia(session)

    code, out, _err = _cli(["tools", "list", "--json"], token=_token(agent), client=client,
                           monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0
    listed = {t["name"] for t in json.loads(out)["tools"]}
    assert listed == _catalog_names(client, agent)
    assert not listed & L4_TOOLS

    code, out, _err = _cli(["--json", "tools", "list"], token=_token(lucia), client=client,
                           monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0
    human_listed = {t["name"] for t in json.loads(out)["tools"]}
    assert human_listed == _catalog_names(client, lucia) - L4_TOOLS

    code, out, _err = _cli(["tools", "list"], token=_token(agent), client=client,
                           monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0 and "list_services" in out and "needs_conversation" in out


# --- 2. call ---------------------------------------------------------------------


def test_call_proxies_the_envelope_and_the_server_stays_the_authority(
    client, session, monkeypatch, capsys
):
    agent = _reception(session)
    conversation_id = _seeded_conversation(session)
    recorder = Recorder(client)

    code, out, err = _cli(
        ["--json", "call", "list_services", "--conversation", str(conversation_id),
         "--args", "{}"],
        token=_token(agent), client=client, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["status"] == "success"
    [sent] = [r for r in recorder.requests if r.url.path == "/agent-tools/call"]
    envelope = json.loads(sent.content)
    assert envelope["request_id"] == sent.headers["X-Request-Id"]
    assert envelope["correlation_id"] == sent.headers["X-Correlation-Id"]
    assert envelope["tool_version"] == "1.0" and envelope["idempotency_key"] is None
    direct = client.post(
        "/agent-tools/call",
        headers={**agent, "X-Request-Id": (r := str(uuid4())), "X-Correlation-Id": (c := str(uuid4()))},
        json={**envelope, "request_id": r, "correlation_id": c},
    )
    assert body["data"] == direct.json()["data"]

    code, _out, err = _cli(
        ["call", "register_contact_profile", "--conversation", str(conversation_id),
         "--args", json.dumps({"full_name": "Ana Prueba"})],
        token=_token(agent), client=client, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0, err
    [mutation] = recorder.bodies("/agent-tools/call")[-1:]
    assert mutation["tool_version"] == "1.1"
    assert UUID(mutation["idempotency_key"]).version == 4

    before = len(recorder.requests)
    code, _out, err = _cli(
        ["call", "list_services", "--conversation", str(conversation_id), "--args", "{nope"],
        token=_token(agent), client=client, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 2 and len(recorder.requests) == before

    code, _out, err = _cli(
        ["call", "confirm_appointment", "--conversation", str(conversation_id), "--args",
         json.dumps({"proposal_id": 1, "confirmation_token": str(uuid4()),
                     "source_message_id": 1})],
        token=_token(agent), client=client, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 1
    assert err.startswith("PERMISSION_DENIED")
    assert recorder.bodies("/agent-tools/call")[-1]["tool_name"] == "confirm_appointment"


# --- 3/4. cobranza flow, decline, me ------------------------------------------------


def test_cobranza_flow_from_the_cli_executes_the_approval_once(
    client, session, monkeypatch, capsys
):
    _overdue(session)
    _airy_id, airy = _airy_caller(session)
    _lucia_id, lucia = _lucia(session)

    def cli(argv, headers):
        return _cli(argv, token=_token(headers), client=client,
                    monkeypatch=monkeypatch, capsys=capsys)

    code, out, err = cli(["runs", "start", "cobranza"], airy)
    assert code == 0, err
    assert "proposed=1" in out

    code, out, err = cli(["inbox", "--json"], lucia)
    assert code == 0, err
    [item] = [i for i in json.loads(out)["items"] if i["kind"] == "collection_reminder"]
    assert len(item["payload_hash"]) == 64
    pid, phash = str(item["id"]), item["payload_hash"]

    code, _out, err = cli(["approve", pid, "--hash", phash], airy)
    assert code == 1
    assert err.startswith("PERMISSION_DENIED")
    session.expire_all()
    assert session.get(AgentProposal, item["id"]).status == "pending"
    session.rollback()
    assert _count(session, OutboundMessage) == 0

    key = str(uuid4())
    code, out, err = cli(["--json", "approve", pid, "--hash", phash, "--idempotency-key", key],
                         lucia)
    assert code == 0, err
    assert json.loads(out)["status"] == "executed"
    assert _count(session, OutboundMessage) == 1

    code, out, err = cli(["approve", pid, "--hash", phash, "--idempotency-key", key], lucia)
    assert code == 0, err
    assert _count(session, OutboundMessage) == 1

    code, _out, err = cli(["approve", pid, "--hash", phash], lucia)
    assert code == 1 and err.startswith("PROPOSAL_NOT_PENDING")
    assert _count(session, OutboundMessage) == 1


def test_decline_and_me(client, session, monkeypatch, capsys):
    _overdue(session)
    _airy_id, airy = _airy_caller(session)
    lucia_id, lucia = _lucia(session)
    assert client.post("/agent-runs", json={"agent_key": "cobranza"},
                       headers={**airy, "Idempotency-Key": str(uuid4())}).status_code == 201
    proposal_id = session.scalar(select(AgentProposal.id))
    session.rollback()

    code, out, err = _cli(["decline", str(proposal_id), "--note", "ya pagó"],
                          token=_token(lucia), client=client, monkeypatch=monkeypatch,
                          capsys=capsys)
    assert code == 0, err
    assert "declined" in out

    code, out, err = _cli(["me", "--json"], token=_token(lucia), client=client,
                          monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0, err
    me = json.loads(out)
    assert me["principal"]["id"] == lucia_id and me["principal"]["type"] == "human"
    assert "proposals.decide" in me["permissions"]


# --- 5. config and transport ---------------------------------------------------------


def test_missing_token_and_unreachable_url(client, session, monkeypatch, capsys):
    recorder = Recorder(client)
    code, out, err = _cli(["me"], token=None, client=client, monkeypatch=monkeypatch,
                          capsys=capsys)
    assert code == 2 and "CONFIG_MISSING" in err and recorder.requests == []

    from odontoflow_cli.main import main

    secret = "of_secret_token_value_123"
    monkeypatch.setenv("ODONTOFLOW_TOKEN", secret)
    monkeypatch.setenv("ODONTOFLOW_URL", "http://127.0.0.1:9")
    code = main(["me"])
    out, err = capsys.readouterr()
    assert code == 3 and err.startswith("TRANSPORT_ERROR")
    assert secret not in out + err


# --- 6. MCP in-process -------------------------------------------------------------


def _api(client, headers):
    from odontoflow_cli.api import OdontoflowApi

    return OdontoflowApi("http://testserver", _token(headers), http_client=client)


def _mcp(server, script):
    from mcp import Client

    async def run():
        async with Client(server) as mcp_client:
            return await script(mcp_client)

    return asyncio.run(run())


def test_mcp_server_proxies_the_catalog_in_process(client, session):
    from odontoflow_mcp.server import StartupError, build_server

    agent = _reception(session)
    conversation_id = _seeded_conversation(session)
    recorder = Recorder(client)
    server = build_server(_api(client, agent))

    async def script(c):
        tools = {t.name for t in (await c.list_tools()).tools}
        services = await c.call_tool(
            "list_services", {"arguments": {}, "conversation_id": conversation_id})
        locations = await c.call_tool(
            "list_locations", {"arguments": {}, "conversation_id": conversation_id})
        args = {"arguments": {"full_name": "Ana Prueba"}, "conversation_id": conversation_id}
        first = await c.call_tool("register_contact_profile", args)
        again = await c.call_tool("register_contact_profile", args)
        other = await c.call_tool(
            "register_contact_profile",
            {"arguments": {"full_name": "Otra Persona"}, "conversation_id": conversation_id})
        return tools, services, locations, first, again, other

    tools, services, locations, first, again, other = _mcp(server, script)
    assert tools == (_catalog_names(client, agent) - L4_TOOLS) | FIXED_MCP_TOOLS
    assert not {n for n in tools if "approve" in n or "decline" in n}
    assert not services.is_error and "services" in services.structured_content
    assert not locations.is_error and "locations" in locations.structured_content
    assert not first.is_error and not again.is_error
    keys = [b["idempotency_key"] for b in recorder.bodies("/agent-tools/call")
            if b["tool_name"] == "register_contact_profile"]
    assert len(keys) == 3
    assert keys[0] == keys[1] != keys[2]
    assert UUID(keys[0]).version == 4
    assert other is not None

    _overdue(session)
    _airy_id, airy = _airy_caller(session)
    airy_server = build_server(_api(client, airy))

    async def airy_script(c):
        denied = await c.call_tool(
            "list_services", {"arguments": {}, "conversation_id": conversation_id})
        run = await c.call_tool("odontoflow_start_cobranza_run", {})
        return denied, run

    denied, run = _mcp(airy_server, airy_script)
    assert denied.is_error and "PERMISSION_DENIED" in denied.content[0].text
    assert not run.is_error and run.structured_content["counts"]["proposed"] == 1

    _lucia_id, lucia = _lucia(session)
    with pytest.raises(StartupError) as refused:
        build_server(_api(client, lucia))
    assert refused.value.code == "HUMAN_TOKEN_REFUSED"


# --- 7. MCP stdio ------------------------------------------------------------------


def test_mcp_stdio_subprocess_lists_the_catalog_and_fails_cleanly(migrated_engine, session):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    agent = _reception(session)
    token = _token(agent)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)  # connections queue from here: deterministic readiness
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_app(migrated_engine), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "odontoflow_mcp"],
            env={"ODONTOFLOW_URL": f"http://127.0.0.1:{port}", "ODONTOFLOW_TOKEN": token},
            cwd=str(REPO_ROOT),
        )

        async def run():
            async with Client(params) as c:
                return {t.name for t in (await c.list_tools()).tools}

        names = asyncio.run(run())
        assert names == (_catalog_names(TestClient(_app(migrated_engine)), agent) - L4_TOOLS) \
            | FIXED_MCP_TOOLS
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()

    failed = subprocess.run(
        [sys.executable, "-m", "odontoflow_mcp"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        env={"ODONTOFLOW_URL": "http://127.0.0.1:9", "ODONTOFLOW_TOKEN": token,
             "PATH": "/usr/bin:/bin"},
    )
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert failed.stderr.startswith("TRANSPORT_ERROR: ")
    assert token not in failed.stderr


# --- 8. boundary -------------------------------------------------------------------

FORBIDDEN = ("app", "sqlalchemy", "psycopg", "alembic")


def test_packages_never_import_the_app_or_the_database():
    probe = (
        "import sys, json, odontoflow_cli.main, odontoflow_cli.api, odontoflow_mcp.server;"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=60, check=True)
    loaded = json.loads(result.stdout)
    leaked = [m for m in loaded if m.split(".")[0] in FORBIDDEN]
    assert leaked == []

    for package in ("odontoflow_cli", "odontoflow_mcp"):
        files = sorted((REPO_ROOT / package).glob("*.py"))
        assert files, package
        for path in files:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    roots = [(node.module or "").split(".")[0]]
                else:
                    roots = []
                assert not set(roots) & set(FORBIDDEN), (path, roots)
                if package == "odontoflow_mcp":
                    assert not (isinstance(node, ast.Call) and getattr(node.func, "id", "")
                                == "print"), path
                    if isinstance(node, ast.ImportFrom):
                        assert node.module != "odontoflow_cli.main", path
