# OdontoFlow CLI and MCP server

Two thin HTTP-only clients over the same API, bearer tokens and permissions as
the UI. They import nothing from `app/` or the database (a test enforces this).
Spec: `docs/superpowers/specs/2026-10-01-erp-mcpcli.md`.

| Package | Entry point | Use it for |
|---|---|---|
| `odontoflow_cli/` | `odontoflow` | people and scripts; works with agent **and** human tokens |
| `odontoflow_mcp/` | `odontoflow-mcp` | Claude or another MCP client; **agent tokens only** |

## Setup

```bash
uv sync --extra mcp              # httpx + mcp (the CLI only needs httpx)
export ODONTOFLOW_URL=http://127.0.0.1:8000     # default
export ODONTOFLOW_TOKEN=...                     # required, never printed
```

Issue credentials with `scripts/issue_credential.py issue --name <principal>
--type <agent|human> --profile <profile>`. The token is shown once.

- `airy-cobranza`, `--type agent --profile collections-agent`: the cobranza demo.
- `n8n-lab-agent`, `--type agent --profile conversation-agent`: the reception tools.
- `Lucía Ramos`, `--type human --profile secretaria`: approves in the inbox.

## CLI

```text
odontoflow [--json] tools list                     # catalog for this token, never L4
odontoflow [--json] call <tool> --args '{...}' --conversation <id>
odontoflow [--json] inbox                          # pending approvals
odontoflow [--json] approve <id> --hash <payload_hash> [--note ..] [--idempotency-key U]
odontoflow [--json] decline <id> [--note ..]
odontoflow [--json] runs start cobranza
odontoflow [--json] me
```

`--json` may come before or after the subcommand and prints the exact server
body. Exit codes: `0` ok, `1` API error (stderr `CODE: message`, e.g.
`PERMISSION_DENIED: ...`), `2` usage or config (`CONFIG_MISSING`), `3` the API
could not be reached (`TRANSPORT_ERROR`).

Every catalog tool currently has `needs_conversation=true`, so `call` needs
`--conversation <id>`. Without it the server answers `INVALID_INPUT: This tool
requires a conversation_id.` `tools list` shows the flag per tool.

A mutation `call` sends a fresh UUIDv4 idempotency key. `approve` sends a fresh
key unless `--idempotency-key` is given. Reusing a key replays the approval, so
no second message is sent.

## MCP server

```bash
odontoflow-mcp                     # stdio (default)
```

Claude Code / Claude Desktop (`mcpServers` entry):

```json
{
  "odontoflow": {
    "command": "/path/to/backend/.venv/bin/odontoflow-mcp",
    "env": {"ODONTOFLOW_URL": "http://127.0.0.1:8000", "ODONTOFLOW_TOKEN": "<agent token>"}
  }
}
```

At startup it calls `GET /me` and **refuses a human token**
(`HUMAN_TOKEN_REFUSED`). If it ran with Lucía's token, every LLM call and run
would be audited as Lucía. It then fetches the catalog once. Each non-L4 tool
becomes one MCP tool with the same name and the parameters `arguments` (the
tool's JSON arguments) and `conversation_id`. It also adds three fixed tools:
`odontoflow_inbox`, `odontoflow_start_cobranza_run` and `odontoflow_me`.
Approve and decline are **never** exposed, because approval is a typed human
act (CLI or UI).

- A mutation tool's idempotency key comes from sha256(`conversation_id`, tool,
  canonical args), the B1 gateway rule. An LLM or client retry therefore
  replays and does not act twice.
- Errors reach the model as tool errors `CODE: message`.
- Startup failures (missing token, 401, unreachable API, human token) exit
  non-zero with one `CODE: message` line on stderr. stdout is the JSON-RPC
  channel and nothing else writes to it.

### Known debt (B4): the airy-cobranza catalog

`airy-cobranza` is not mapped in `AGENT_KEY_BY_DISPLAY_NAME`, so it falls back
to the `reception` allowlist. Its catalog therefore **lists the 13 reception
tools, and every one of them returns `PERMISSION_DENIED`** for that token
(`collections-agent` has no `conversations.read`). These tools are not cobranza
abilities. With the airy token, use only `odontoflow_start_cobranza_run`,
`odontoflow_inbox` and `odontoflow_me`. B4 moves the allowlist to agent
definitions.

### streamable-http (opt-in, untested)

`odontoflow-mcp --transport streamable-http --port 3001` always binds
`127.0.0.1` and keeps the SDK's default DNS-rebinding protection. **There is no
caller authentication**: any local process that reaches the port acts with the
process token's authority. Prefer stdio. OAuth for MCP is out of scope.

## Demo script: cobranza "behind the scenes"

```bash
# terminal, as the collections agent
ODONTOFLOW_TOKEN=$AIRY odontoflow runs start cobranza        # run 7 cobranza: completed ... proposed=1
# terminal, as Lucía
ODONTOFLOW_TOKEN=$LUCIA odontoflow inbox                      # <id> collection_reminder pending <hash> ...
ODONTOFLOW_TOKEN=$AIRY  odontoflow approve <id> --hash <hash> # exit 1: PERMISSION_DENIED: ...
ODONTOFLOW_TOKEN=$LUCIA odontoflow approve <id> --hash <hash> # proposal <id>: executed (1 WhatsApp queued)
```

From Claude over MCP with the airy token, ask it to "run the cobranza sweep and
show me the inbox". It calls `odontoflow_start_cobranza_run` and then
`odontoflow_inbox`. It cannot approve; Lucía does that from the CLI or the UI.
