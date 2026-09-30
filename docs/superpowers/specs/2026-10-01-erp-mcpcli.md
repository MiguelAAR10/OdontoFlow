---
title: "ERP MCPCLI — Puertas agénticas: servidor MCP + CLI 'odontoflow' sobre el catálogo HTTP"
status: active
---
Sources: plan §2 (same deterministic services for every actor; agents propose, humans approve; L4 never for agents), §5 MCPCLI; B1 catalog `app/agent_tools/service.py:206` (`list_agent_tools`), `:128` (`call_agent_tool`), `registry.py:382` (`allowed_tools`); B1 stable key `sales_agent/gateway.py:45-67`; B2 `app/proposals/router.py:39-130`; COB `app/agents_runtime/router.py:28`; IDN `app/iam/router.py:53` (`GET /me`). SDK: `mcp` 2.2.0, checked in Context7 (`/websites/py_sdk_modelcontextprotocol_io`) and against the installed package on 2026-09-30. **Pinned imports:** `from mcp import Client`, `from mcp.server import MCPServer`, `from mcp.server.mcpserver.exceptions import ToolError`, `from mcp.client.stdio import StdioServerParameters` (`Client` accepts an `MCPServer` for in-memory and a `StdioServerParameters` for stdio). Code at HEAD 0bd789b.

## Telos
External agents (Claude over MCP, scripts) and the team drive the ERP through the **same** HTTP API, bearer and permissions as the UI. Demo: from a terminal, `odontoflow runs start cobranza` → `inbox` → `approve` as Lucía; from Claude (airy-cobranza token), `odontoflow_start_cobranza_run`, `odontoflow_inbox`, `odontoflow_me` — never an approval.

## Hypothesis of value
Everything server-side exists: catalog filtered per token (L4 dropped for agents, `registry.py:382-387`), stable envelope, 403 on denial (`service.py:164-168`), human-only approve (`_require_human`, `proposals/service.py:326`). What is missing is a thin **HTTP-only client** in two shapes. No new route, table, permission or migration: if anything server-side must change, the card is wrong.

## Observable metric
One demo session: `odontoflow runs start cobranza` (agent token) → `proposed=1`; `odontoflow inbox --json` (secretaria) lists the S/ 180 `collection_reminder`; `odontoflow approve <id> --hash <h>` → `executed`, exactly 1 `outbound_messages` row; the same `approve` with the agent token → exit 1, `PERMISSION_DENIED`. Claude (stdio) sees `N + 3` tools, where `N` = catalog-for-token minus L4 and 3 = the fixed tools.

## Known server debt (documented, not fixed here)
`airy-cobranza` is not in `AGENT_KEY_BY_DISPLAY_NAME`, so it falls back to `DEFAULT_AGENT_KEY='reception'` (`registry.py:356-379`): its catalog lists the 13 non-L4 reception tools, and every one of them returns 403 for that token (no `conversations.read`). That is B4 debt; no server change is allowed in this card. `docs/agents/mcp-cli.md` says so, and the Claude demo uses only the three fixed tools with that token.

## Cut criteria
Stop and re-plan if the packages need to import `app.*`, SQLAlchemy or psycopg; if any server file (`app/**`, migrations) must change; if the SDK cannot register a tool at startup via `MCPServer.add_tool`; if the catalog cannot be proxied without per-tool hand-written code.

## Contract
**Packages** (new, stdlib + `httpx` + `mcp` only): `odontoflow_cli/` (`api.py` shared HTTP client — **no printing**, `main.py` argparse CLI and its output helpers, `__main__.py`) and `odontoflow_mcp/` (`server.py` `build_server(api) -> MCPServer`, `__main__.py`). `odontoflow_mcp` imports only `odontoflow_cli.api`, never `odontoflow_cli.main`. `pyproject.toml`: **one** extra `mcp = ["httpx>=0.27", "mcp>=2.2,<3"]` (via `uv add --optional`); the CLI only needs `httpx`, already in that extra, the `agent` extra and the dev group, so there is no separate `cli` extra. Console scripts `odontoflow = "odontoflow_cli.main:main"`, `odontoflow-mcp = "odontoflow_mcp.__main__:main"`; `packages.find.include` gains both. Dev group gains `mcp` so tests import it.

**`OdontoflowApi(base_url, token, http_client: httpx.Client | None = None)`** (injectable client, same pattern as `sales_agent/gateway.py:79`). Config: `ODONTOFLOW_TOKEN` (required; missing → exit 2 `CONFIG_MISSING`, token never printed), `ODONTOFLOW_URL` (default `http://127.0.0.1:8000`). Every request sends `Authorization: Bearer`, fresh UUIDv4 `X-Request-Id` + `X-Correlation-Id`. Methods → routes (no others):

| CLI command | MCP tool | Route |
|---|---|---|
| `tools list` | (MCP `tools/list`) | `GET /agent-tools/catalog`, then drop `level == "L4"` client-side (defense in depth: human catalogs include L4, `service.py:210`) |
| `call <tool> --args JSON [--conversation ID]` | one MCP tool per catalog entry, same name | `POST /agent-tools/call` envelope: `tool_version` from the descriptor, `request_id`/`correlation_id` = the trace headers (`_validate_trace`, `service.py:41`), `idempotency_key` null for `effect=read`. Mutation key: CLI → fresh UUIDv4; MCP → **stable** UUIDv4 from sha256(`conversation_id:tool_name:canonical args`), the B1 gateway rule, so a client/LLM retry replays instead of producing a second effect. A tool absent from the caller's catalog is still sent (as a `1.1` mutation envelope) so the **server** answers 403, never the client |
| `inbox` | `odontoflow_inbox` | `GET /agent/inbox` (server default: pending) |
| `approve <id> --hash H [--note] [--idempotency-key U]` | — (not exposed) | `POST /agent/proposals/{id}/approve` + `Idempotency-Key` (given, else fresh UUIDv4; the flag exists for a deliberate replay) |
| `decline <id> [--note]` | — (not exposed) | `POST /agent/proposals/{id}/decline` |
| `runs start cobranza` | `odontoflow_start_cobranza_run` | `POST /agent-runs` `{agent_key:"cobranza"}` + fresh `Idempotency-Key` (a rerun dedupes server-side) |
| `me` | `odontoflow_me` | `GET /me` |

Every current catalog tool has `needs_conversation=true` (`registry.py:284`); without `--conversation` / `conversation_id` the server returns `INVALID_INPUT "This tool requires a conversation_id."`. The CLI shows `needs_conversation` in `tools list` and the docs say so; it does not pre-check it (the server is the authority).

**Client-side policies (the only two):** (1) L4 descriptors are dropped from `tools list` and from MCP registration; (2) MCP exposes no approve/decline. Everything else — permissions, allowlist, L4 refusal, human-only approval — is the server's, and a test proves an agent-token `call confirm_appointment` still yields the server's 403.

**Decision (defaulted, recorded):** MCP exposes no approve/decline. An LLM holding a human token must not click "approve" (plan §2); approval stays a typed human act (CLI/UI). The server still refuses agent approval with 403 regardless.

**MCP server.** `build_server(api)` at startup calls `GET /me` and **refuses** a `principal.type == "human"` token (`StartupError HUMAN_TOKEN_REFUSED`): otherwise the LLM's calls and runs would be audited as the human (`triggered_by_principal_id`, `agents_runtime/service.py:141`). The CLI stays usable with human tokens. Then it fetches the catalog **once** and, per non-L4 descriptor, `mcp.add_tool(_proxy(api, d), name=d.name, description=d.description + JSON of d.arguments_schema)`, where `_proxy` is a factory that returns a **fresh closure per descriptor** (no loop late binding), `fn(arguments: dict[str, Any] | None = None, conversation_id: int | None = None) -> dict`; plus the three fixed tools above. Tool result = the server's `data` (success) or a `ToolError` `"<code>: <message>"` (envelope `status=error`, non-2xx or transport).
- **stdout is the JSON-RPC channel.** `odontoflow_mcp` never prints to stdout; SDK logging goes to stderr. A startup failure (missing token, 401, connection refused, human token) exits non-zero with only `<code>: <message>` on stderr; the token never appears.
- `odontoflow-mcp [--transport stdio|streamable-http] [--port 3001]`, `stdio` default. streamable-http is opt-in and **untested**: it binds `127.0.0.1` (not configurable) with the SDK's default DNS-rebinding protection (`TransportSecuritySettings` auto-enabled for localhost; never overridden). It has no caller authentication: any local process that reaches the port inherits the token's authority — documented in `docs/agents/mcp-cli.md`.

**Output & errors.** Human text by default; `--json` (before or after the subcommand) prints the exact server body. Exit codes: `0` 2xx and envelope `success`; `1` API error (non-2xx or envelope `error`) → stderr `PERMISSION_DENIED: <message>` (or the body on stdout under `--json`); `2` usage/config; `3` transport (connection refused/timeout, `TRANSPORT_ERROR`). Codes are the server's six plus B2/COB ones, passed through, never re-mapped. Example (`odontoflow --json approve 41 --hash 9f…` with an agent token, exit 1):
```json
{"error":{"code":"PERMISSION_DENIED","message":"The principal is not authorized to perform this action.","details":{}}}
```

## Invariants (what PostgreSQL enforces — all pre-existing, none added)
- Execute-once: B2 UNIQUE `execution_key` + `uq_outbound_messages_organization_idempotency`; replays via `run_idempotent_command` receipts (same key → `Idempotent-Replay: true`, no second effect).
- Tenant isolation: the bearer resolves to one org; composite tenant FKs make cross-org rows impossible; the client sends no organization id.
- Authority: `require_permission` + `_require_human` + audit (`record_event`) live server-side; the packages hold only the two client-side policies above.

## Acceptance tests (written first; `tests/test_mcp_cli.py`, real PostgreSQL, one pytest process; `TestClient(app)` injected as `http_client`)
Tokens: **reception agent** = `conversation-agent` profile (`n8n-lab-agent`-style) + a seeded conversation (`_seed_reception`); **airy** = `airy-cobranza` with `collections-agent` (`_airy_caller` from `test_collections_sweep.py`); **Lucía** = `secretaria` (`_lucia`). An autouse fixture `monkeypatch.delenv("AGENT_COBRANZA_ENABLED")` keeps the kill switch out.
1. `tools list --json` with the reception agent token == server catalog names for that token (no L4); with Lucía's token == server catalog minus the three L4 `confirm_*`.
2. Reception agent: `call list_services --conversation C --args '{}'` → exit 0, data equals a direct `/agent-tools/call`; the envelope trace ids equal the headers; `call register_contact_profile` sends a UUIDv4 key; invalid `--args` JSON → exit 2, no request; agent `call confirm_appointment` → exit 1, `PERMISSION_DENIED` from the server (the request was sent).
3. Cobranza flow: `runs start cobranza` (airy) → `proposed=1`; `inbox --json` (Lucía) shows it with `payload_hash`; `approve` with airy → exit 1, stderr starts `PERMISSION_DENIED`, proposal still `pending`, 0 outbound; `approve` as Lucía → `executed`, 1 outbound; same `--idempotency-key` again → replay, still 1; a new key → exit 1 with the server's status error, still 1.
4. `decline` as Lucía on another pending proposal → `declined`; `me --json` returns the principal/permissions of the token.
5. Missing `ODONTOFLOW_TOKEN` → exit 2, no HTTP call; unreachable `ODONTOFLOW_URL` → exit 3; token absent from all output.
6. MCP in-process (`async with Client(build_server(api))`, via `asyncio.run`), reception agent: `list_tools` names == catalog-for-token minus L4 + the 3 fixed tools, no `approve`/`decline`; `list_services` and `list_locations` each return their own data (catches late binding); the same `register_contact_profile` call twice sends the same UUIDv4 key, other args a different one. Airy: `list_services` → `is_error` with `PERMISSION_DENIED` (B4 debt), `odontoflow_start_cobranza_run` → `proposed=1`. Lucía's token → `build_server` raises `HUMAN_TOKEN_REFUSED`.
7. MCP stdio: the app is served by uvicorn in a thread on a **pre-bound** `127.0.0.1:0` socket (`server.run(sockets=[sock])`: the kernel queues connections from `listen()`, so readiness is deterministic with no sleep/poll); shut down with `server.should_exit = True` + `thread.join`. The subprocess (`python -m odontoflow_mcp`, SDK stdio client) gets only `ODONTOFLOW_URL`/`ODONTOFLOW_TOKEN` over the SDK's safe default env → one initialize + `list_tools` returns the catalog. Failure path: unreachable URL → non-zero exit, empty stdout, stderr `TRANSPORT_ERROR: …`, no token.
8. Boundary: subprocess imports `odontoflow_cli.main`, `odontoflow_mcp.server` and asserts no `app`, `app.*`, `sqlalchemy*`, `psycopg*`, `alembic*` in `sys.modules`; an AST scan of both packages finds no such import and no `print(` in `odontoflow_mcp`.

## Out of scope
OAuth/remote auth for MCP, hosting, n8n workflow changes, MCP approve/decline, MCP resources/prompts, per-tool typed Python signatures, live catalog refresh (`tools/list_changed`), token storage/keyring, shell completion, inbox filters, new server routes or permissions, the airy agent_key mapping (B4), OpenAPI regeneration (no route changes). Deliverables besides code/tests: `docs/agents/mcp-cli.md` (setup, Claude Desktop/Code stdio config, demo script, B4 debt, streamable-http risk), one `CHANGELOG.md` entry, `uv.lock`.
