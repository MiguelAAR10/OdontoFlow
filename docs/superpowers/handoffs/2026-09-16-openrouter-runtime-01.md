# OPENROUTER-RUNTIME-01 — Provider & Local Environment Bootstrap

Date: 2026-09-16

Base: `22e2b51011a83fbf1e2c745aecda29b44265c71e`

Status: **READY_FOR_OWNER_OPENROUTER_KEY**

## Provider boundary

The Sales Agent still uses `langchain.chat_models.init_chat_model` and the
already-installed `langchain-openai` integration. The configured provider is
`openrouter`, but the implementation passes `model_provider="openai"` to the
existing OpenAI-compatible adapter with the configured OpenRouter base URL and
key. LangGraph, tools, gateway, PostgreSQL, sandbox transport, and booking
policy remain provider-independent. The OpenRouter path explicitly uses the
OpenAI Chat Completions surface (`use_responses_api=False`) for stable typed
tool calling.

No `langchain-openrouter` package or other dependency was required. `pyproject.toml`
and `uv.lock` were not changed.

## Exact configuration

Committed `.env.example` names:

```text
SALES_AGENT_MODEL_PROVIDER=openrouter
SALES_AGENT_MODEL=deepseek/deepseek-v4-flash-0731
SALES_AGENT_MODEL_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=
SALES_AGENT_V0_CREDENTIAL=
SALES_AGENT_DATABASE_URL=postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent
SALES_AGENT_BACKEND_URL=http://127.0.0.1:8000
SANDBOX_BACKEND_URL=http://127.0.0.1:8000
SANDBOX_RECEIVER_URL=http://127.0.0.1:8000/internal/sandbox/receive
SANDBOX_CHANNEL_ACCOUNT_EXTERNAL_ID=sandbox-local
SANDBOX_INBOUND_TOKEN=
SANDBOX_DISPATCHER_TOKEN=
SANDBOX_REQUEST_TIMEOUT_SECONDS=10.0
```

`OPENROUTER_API_KEY` is the only provider secret read for the default
OpenRouter configuration. `OPENAI_API_KEY` is not used as an OpenRouter
fallback. Explicit native OpenAI configuration remains supported with
`SALES_AGENT_MODEL_PROVIDER=openai`, `SALES_AGENT_MODEL=...`, and
`OPENAI_API_KEY`.

The optional manual OpenRouter model is `meta/muse-spark-1.3`; it uses the same
server-side provider, base URL, and key settings. There is no automatic
cross-model fallback.

## Local environment

Setup command, from `odontoflow-backend`:

```bash
./.venv/bin/python scripts/bootstrap_openrouter_local.py
```

The command targets only loopback PostgreSQL at port `5434`, applies the
committed migration head, creates/prepares the separate
`odontoflow_agent` memory database, loads the existing fictitious reception
catalog, creates the `sandbox-local` channel, and writes a managed override
block to ignored `.env.local`. Existing lines outside that block—including
owner-supplied secrets and unrelated settings—are preserved. Re-running the
command reuses valid local credentials and does not rotate them. The local env
file is restricted to mode `600`.

The bootstrap completes without a model or OpenRouter request.

Credential-free preflight command:

```bash
./.venv/bin/python scripts/preflight_sales_agent.py
```

It reads the current ignored env file, prints only sanitized configuration and
booleans, and makes no network request. Before the owner key is present, the
observed output is:

```text
provider=openrouter
model=deepseek/deepseek-v4-flash-0731
base_url_configured=true
openrouter_api_key_configured=false
sales_agent_credential_configured=true
postgres_configured=true
sandbox_configured=true
ready_for_real_model_smoke=false
```

After the local environment was refreshed, the current sanitized preflight
reports `ready_for_real_model_smoke=true`; the non-empty key value is not
recorded or displayed, and no provider request was made.

## Local Sales Agent credential

`SALES_AGENT_V0_CREDENTIAL` was issued through the existing IAM path:
`_resolve_principal` → `_assign_profile(..., profile="sales-agent-v0")` →
`issue_credential`. It is bound to the bootstrap organization and an `agent`
principal. The sandbox bootstrap also uses the existing `n8n-inbound` and
`outbound-dispatcher` profiles for `SANDBOX_INBOUND_TOKEN` and
`SANDBOX_DISPATCHER_TOKEN`. No credential value is recorded here or printed.

The local validation confirmed all three credentials resolve to organization 1
with the required permission sets, and the `sandbox-local` channel is active.

## Verification

TDD red phase:

```text
./.venv/bin/python -m pytest -q tests/test_sales_agent_provider.py tests/test_sales_agent_preflight.py tests/test_openrouter_bootstrap.py
```

Initially failed during collection because the new provider/preflight/bootstrap
interfaces did not exist.

Focused configuration/preflight/bootstrap pack:

```text
10 passed, 1 warning
```

Focused Sales Agent, sandbox, reception, booking, and messaging pack:

```text
./.venv/bin/python -m pytest -q tests/test_sales_agent_provider.py tests/test_sales_agent_preflight.py tests/test_openrouter_bootstrap.py tests/test_sales_agent_w3.py tests/test_sales_agent_w4.py tests/test_sales_agent_auth.py tests/test_sandbox_inbound.py tests/test_sandbox_outbound.py tests/test_reception_agent_phase5.py tests/test_agent_booking_phase4.py tests/test_messaging_phase2.py
85 passed, 2 warnings
```

Additional checks:

```text
./.venv/bin/ruff check sales_agent/config.py sales_agent/runtime.py scripts/bootstrap_openrouter_local.py scripts/preflight_sales_agent.py tests/test_sales_agent_provider.py tests/test_sales_agent_preflight.py tests/test_openrouter_bootstrap.py
All checks passed

DATABASE_URL=<loopback local core> ./.venv/bin/alembic current
0019 (head)

agent-memory tables present: 3
```

No paid model request was authorized or made. Full-suite evidence:

```text
./.venv/bin/python -m pytest -q
560 passed, 21 warnings in 574.51s (0:09:34)
```

## Commit

One task commit; exact SHA is recorded in the close-out `HEAD` and final task
report.

## Remaining owner action

1. Open ignored `.env.local`.
2. Set `OPENROUTER_API_KEY=...`.
3. Run `./.venv/bin/python scripts/preflight_sales_agent.py`.
4. Proceed only when it reports `ready_for_real_model_smoke=true`.

Do not start `SANDBOX-REAL-MODEL-01` automatically.
