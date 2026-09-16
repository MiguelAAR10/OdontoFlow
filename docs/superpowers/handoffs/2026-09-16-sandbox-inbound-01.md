# SANDBOX-INBOUND-01 — Technical handoff

Date: 2026-09-16

Base: `872ddd2915be186fdda8f5bb195b142c49bd6843`

Status: **PASS — controlled sandbox inbound-to-outbound loop verified; no next
activity started**

## Result

The development-only local loop now composes the existing authenticated
boundaries without creating a second messaging domain:

```text
explicit provider=sandbox event
  → authenticated POST /internal/messages/inbound
  → canonical Conversation/Message persistence and provider dedupe
  → authenticated POST /sales-agent/turn with the issued agent principal
  → existing typed tools and fail-closed booking boundary
  → authenticated canonical sandbox outbound persistence
  → existing deliveries.manage sandbox consumer
  → authenticated loopback receiver and durable sandbox receipt
  → existing outbound settlement
```

`integrations/sandbox/sender.py` is a development-only HTTP adapter. It owns no
database state, accepts only text events with `schema_version=1.0` and
`provider=sandbox`, requires both server-issued inbound and Sales Agent
credentials, and passes only canonical IDs to the agent route. A canonical
duplicate stops before the agent turn and outbound command, preserving the
existing WF-01 replay semantics. Transport retries reuse the same inbound or
outbound idempotency key; the mutating Sales Agent turn is not retried.

## Database and security evidence

The new real-PostgreSQL scenario seeds the existing fictitious reception demo
catalog, creates one sandbox channel, and issues separate least-privilege
credentials through the existing IAM profiles. One booking-intent event then
produced:

- exactly one canonical inbound `Message` and one conversation;
- one sandbox outbound `Message`/`OutboundMessage`, containing the Sales Agent
  reply and reaching `delivered` through the existing consumer;
- one durable `sandbox_delivery_receipts` row and one
  `outbound.sandbox.received` audit event;
- one pending appointment proposal, zero confirmed proposals, and zero
  appointments because the fake model attempted same-turn confirmation and
  the existing agent fail-closed guard returned `INVALID_INPUT`;
- no additional inbound, agent turn, outbound, or receipt after replaying the
  same provider message ID.

The focused negative tests also show that missing/invalid inbound credentials
are rejected before persistence, a tenant-1 credential cannot use a tenant-2
sandbox channel, missing sender credentials fail closed, and the sender rejects
both `test` and `whatsapp` before contacting canonical ingress. Existing
Sales Agent authentication, sandbox consumer tenant/provider isolation,
outbound retry/settlement, and valid two-message/human-confirmation tests
remain green in the focused and full suites.

## Reproducible local startup and smoke

Run from `odontoflow-backend`. These commands are development-only and must
target the local PostgreSQL CORE instance, never production:

```bash
# Verify the existing CORE and apply the already-committed head locally.
docker ps --filter name=odontoflow-db-1 --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
pg_isready -h 127.0.0.1 -p 5434 -U odontoflow -d odontoflow
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  ./.venv/bin/alembic upgrade head

# Optional local API process for the existing sandbox dispatcher/receiver.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
APP_ENV=development ERP_ANONYMOUS_COMPAT=true INTEGRATION_API_ENABLED=true \
API_HOST=127.0.0.1 API_PORT=8000 ./.venv/bin/python -m app.run

# The supported no-paid-model smoke uses the existing in-process fake model,
# disposable real PostgreSQL databases, FastAPI TestClient boundaries, and
# the checked-in SandboxConsumer. It creates/cleans its own test data.
./.venv/bin/python -m pytest -q tests/test_sandbox_inbound.py

# If an outbound sandbox row is already queued in the local API database, the
# existing authorized one-shot consumer remains the delivery command:
export SANDBOX_BACKEND_URL=http://127.0.0.1:8000
export SANDBOX_RECEIVER_URL=http://127.0.0.1:8000/internal/sandbox/receive
export SANDBOX_DISPATCHER_TOKEN='<server-issued outbound-dispatcher token>'
./.venv/bin/python scripts/sandbox_dispatcher.py --limit 10
```

The focused test is the complete reproducible visible-response proof without
printing or requiring model-provider credentials. The repository does not
provide a separate standalone fake-model Sales Agent process; starting
`sales_agent.api:app` without an injected fake model would use its configured
real-model path and is deliberately not part of this smoke.

## Validation

Intentional red phase before implementation:

```text
./.venv/bin/python -m pytest -q tests/test_sandbox_inbound.py
2 failed, 1 passed, 2 warnings
ModuleNotFoundError: No module named 'integrations.sandbox.sender'
```

After implementation and fixture correction:

```text
./.venv/bin/python -m pytest -q tests/test_sandbox_inbound.py
5 passed, 2 warnings

./.venv/bin/python -m pytest -q tests/test_sandbox_inbound.py \
  tests/test_sandbox_outbound.py tests/test_sales_agent_w4.py \
  tests/test_sales_agent_auth.py tests/test_reception_agent_phase5.py \
  tests/test_agent_booking_phase4.py tests/test_messaging_phase2.py
59 passed, 2 warnings

./.venv/bin/python -m pytest -q
550 passed, 21 warnings in 444.49s (0:07:24)
```

The current collection is **550**, compared with the historical **527-pass**
baseline and the previous outbound-task collection of 545. There were no
failures or skips. The 21 warnings are the existing Starlette TestClient
deprecation and Alembic `path_separator` deprecation warnings.

Focused Ruff and `git diff --check` passed for the new Python files. No schema
or OpenAPI change was needed. Protected paths `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py`, existing migrations, and `../../medistock`
were not changed; unrelated working-tree modifications remain unstaged.

## Files in the bounded change

- `integrations/sandbox/sender.py` — strict, credentialed sandbox inbound
  adapter and canonical sequence orchestration;
- `tests/test_sandbox_inbound.py` — real-PostgreSQL loop, replay, auth,
  tenant, and provider-isolation regressions;
- `CHANGELOG.md` — shipped-change record;
- this handoff.

Planning living brief:
`../odontoflow-planning/docs/handoffs/plans/2026-09-16-sandbox-inbound-01.md`

No inbound channel integration, WhatsApp, n8n deployment, paid model,
automatic booking, consent mechanism, IAM redesign, or production write was
introduced. No next activity is authorized or started automatically.
