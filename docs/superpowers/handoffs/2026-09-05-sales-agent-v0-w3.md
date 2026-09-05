# W3 Handoff — Sales Agent V0 runtime

Date: 2026-09-05
Workstream: W3 — Sales Agent Runtime
Owner: GPT-5.6 Luna Max via Orca
Status: implementation and verification complete; this document ships in the
single W3 commit

## Objective

Add the smallest approved Sales Agent V0 runtime as an optional, separate
process. The runtime must use LangChain `create_agent`, durable synchronous
LangGraph `PostgresSaver` working memory in a separate `odontoflow_agent`
database, exactly seven authenticated typed gateway tools, bounded structured
turns, content-free telemetry, and deterministic fake-model coverage, while
leaving the canonical `app` process and business services authoritative.

No Alembic revision, canonical business table, protected application module,
deterministic scheduling authority, W4 workflow, or planning cycle was added or
changed.

## Exact runtime and memory contract

- `sales_agent/` is a top-level package outside `app/` and is installed only
  with the `agent` optional dependency group in `pyproject.toml`.
- `sales_agent.api:app` is the separate process entrypoint. The canonical
  `app` package has no LangChain or LangGraph imports, and importing the Sales
  Agent API does not import those optional libraries until the runtime is
  actually built.
- `SalesAgentRuntime` calls LangChain `create_agent` only. It does not define a
  custom `StateGraph`, multi-agent graph, `MemorySaver`, or `InMemorySaver`.
- Structured final output uses
  `response_format=ToolStrategy(SalesAgentResponse)` and reads
  `result["structured_response"]`. The typed response contains exactly
  `reply`, `outcome` (`continue`, `proposed`, `confirmed`, or `handoff`), and
  `handoff`; the HTTP response adds the two request identifiers.
- Each invoke passes
  `config={"configurable": {"thread_id": str(conversation_id)},
  "recursion_limit": N}`. `N` is configurable through
  `SALES_AGENT_RECURSION_LIMIT`, bounded to 1–100, and an exhausted graph
  raises into a safe typed `request_human_handoff` call with a deterministic
  handoff response. There are no unbounded retries.
- `PostgresAgentMemory.open()` keeps one synchronous `PostgresSaver` context
  open for the process lifetime and exposes its checkpointer to the runtime.
  `PostgresAgentMemory.setup()` and `setup_agent_memory()` are explicit
  provisioning seams; normal process startup does not silently provision the
  database. SQLAlchemy PostgreSQL URLs are normalized for psycopg conninfo,
  and known canonical database names are rejected.
- The checkpointer database URL defaults to
  `postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent`
  but is configurable with `SALES_AGENT_DATABASE_URL` (or
  `AGENT_DATABASE_URL`). It is never the canonical application database.

## Exact tool and HTTP contracts

`build_v0_tools()` returns exactly these seven `@tool` wrappers, in this order:

1. `get_reception_context`
2. `list_services`
3. `list_locations`
4. `query_available_slots`
5. `propose_appointment`
6. `confirm_appointment`
7. `request_human_handoff`

Every wrapper is conversation-bound and delegates only to
`BackendGateway.call_tool()`. The gateway sends authenticated `POST
/agent-tools/call` requests with `Authorization: Bearer <SALES_AGENT_V0_CREDENTIAL>`,
the required `conversation_id`, typed `tool_version`, UUID `request_id`, UUID
`correlation_id`, and typed `arguments`. Read calls use version `1.0` and
`idempotency_key: null`; mutations use version `1.1` and a fresh UUIDv4
idempotency key in both the envelope and `Idempotency-Key` header. Backend
success/error envelopes remain typed `AgentToolResult` values; HTTP errors
become safe typed gateway errors.

The wrappers deliberately do not expose contact-profile registration,
practitioner listing, contact appointments, cancellation, rescheduling,
promotions, prices, SQL, or arbitrary tools. Appointment duration, slot
validity, conflicts, confirmation, and all other business rules remain in the
canonical backend gateway.

`POST /sales-agent/turn` accepts:

```json
{
  "conversation_id": 123,
  "latest_inbound_message_id": 456
}
```

The runtime loads that inbound message through the existing typed
`get_reception_context` gateway surface, invokes the conversation thread, and
returns a typed response with `conversation_id`,
`latest_inbound_message_id`, `reply`, `outcome`, and `handoff`.

## Three-state boundary

| State | Owner | Storage | Authority |
|---|---|---|---|
| Conversation / Message and audit transcript | `app` | canonical PostgreSQL database | Canonical record of what was said |
| Agent working/checkpoint state | `sales_agent` | separate `odontoflow_agent` PostgreSQL database, LangGraph `PostgresSaver`, one thread per conversation | Disposable working memory; no business authority |
| Business state: catalog, availability, appointments, and future clinical/finance/inventory state | `app` deterministic services | canonical PostgreSQL database | Only business truth |

The Sales Agent performs no canonical SQL or ORM access. Dropping the separate
agent-memory database therefore leaves canonical organization,
conversation/message, appointment, and audit rows intact; the acceptance test
seeds synthetic canonical rows and verifies their counts before and after the
drop.

## Telemetry and safety

Each turn emits one structured event with exactly these fields:

`conversation_id`, `model`, `input_tokens`, `output_tokens`, `model_calls`,
`tool_calls`, `tool_failures`, `latency_ms`, `outcome`.

The current-compatible `@wrap_tool_call` middleware counts tool calls and typed
error envelopes, converts unexpected tool exceptions into a generic safe
`ToolMessage`, and never logs message content, prompts, tool arguments,
credentials, tool-call IDs, or PII. Model middleware reads usage metadata only
when a provider/fake model supplies it.

## TDD and verification evidence

The focused W3 test file was written before the implementation and initially
observed the expected missing-package RED failures. A later focused RED test
also caught the zero recursion-limit fallback; the implementation then passed
the boundary test.

Final focused runtime suite:

```text
.venv/bin/python -m pytest -q tests/test_sales_agent_w3.py
16 passed, 1 warning
```

The focused tests prove:

- same `conversation_id` resumes the same persisted PostgreSQL checkpointer
  thread;
- different conversation IDs never share checkpoint messages;
- deleting the separate agent database preserves canonical organization,
  conversation, message, appointment, and audit state;
- only the seven V0 tools exist, with cancellation/rescheduling and other
  forbidden capabilities unavailable;
- every wrapper uses the authenticated typed `/agent-tools/call` envelope;
- structured response, `thread_id`, and recursion-limit contracts are present;
- recursion exhaustion invokes typed human handoff safely;
- tool failures are counted without exposing tool details;
- telemetry has all nine required fields and no message/tool content;
- the optional dependency group and `app` import guard work without importing
  LangChain/LangGraph.

Required final full suite:

```text
.venv/bin/python -m pytest -q
494 passed, 20 warnings in 422.23s (0:07:02)
```

Additional final checks:

```text
.venv/bin/python -m compileall -q sales_agent tests/test_sales_agent_w3.py  PASS
git diff --check                                                        PASS
```

No live model provider was called. All agent runtime tests use synthetic
conversation/message values, fake chat models, and local PostgreSQL databases.

## Files changed

- `sales_agent/__init__.py`
- `sales_agent/api.py`
- `sales_agent/config.py`
- `sales_agent/gateway.py`
- `sales_agent/memory.py`
- `sales_agent/runtime.py`
- `sales_agent/schemas.py`
- `sales_agent/tools.py`
- `tests/test_sales_agent_w3.py`
- `pyproject.toml`
- `CHANGELOG.md`
- this handoff

The protected files `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py`, `app/catalog/schemas.py`, existing ERP
schemas, and `alembic/versions/**` were not modified. Pre-existing unrelated
checkout changes in `AGENTS.md`, `.agents/`, `.claude/`, `.playwright-mcp/`,
`docs/integration/TEAM_SYNC_AND_BRAND_TRANSFER.md`, and `skills-lock.json`
were preserved and were not staged.

## Commit and push

Base commit before W3 editing:

```text
5dda97375891fdc63d7ca96c46ae2c9c50542b69
```

One normal W3 commit is created with the repository convention:

```text
feat: add sales agent runtime
```

The exact result commit is the `HEAD` reported after this commit; it contains
this handoff. The branch is pushed normally with `git push origin main` (no
force-push, reset, rebase, squash, or amend). The worker completion report
records the resulting commit SHA and push output.

## Synthetic-only limitation and blockers

CI and this handoff prove deterministic contracts with fake models and
synthetic conversations only. A deployed process still needs the separately
provisioned `odontoflow_agent` database, a valid `sales-agent-v0` credential,
and a configured live model provider; none of those external clinic/provider
connections were exercised here.

There are no implementation blockers for W3. W4 workflow wiring and any live
provider/infrastructure setup remain out of scope and unauthorized by this
workstream.
