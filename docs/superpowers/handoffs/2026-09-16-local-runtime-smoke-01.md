# LOCAL-RUNTIME-SMOKE-01 — Local runtime smoke handoff

Date: 2026-09-16
Base: `55f22cce3a8e50f2f18d7b2fce258cbe953ad33`
Status: **PASS — local safe path verified; no next activity started**

## Scope and repository state

This activity established a reproducible local smoke procedure for the
existing Lead → Appointment components. It did not change booking policy,
agent authorization, schema, migrations, channels, n8n, deployment, or
production data.

At intake, the backend was on `main` at
`55f22cce3a8e50f2f18d7b2fce258cbe953ad33` (`fix: fail closed agent booking
confirmation`), with the prior AGENT-CONFIRM-FAIL-CLOSED-03 handoff already
present. The backend worktree contained pre-existing unrelated modified and
untracked paths (`.gitignore`, `AGENTS.md`, `.agents/`, `.claude/`,
`.playwright-mcp/`, architecture/review documents, prior intent/deployment
handoffs, and `skills-lock.json`). None were staged or changed by this
activity. The planning worktree was also already dirty; its existing control
plane was preserved.

The existing local docs are not a single current recipe: `README.md` still
describes an older 384-test/0008 state, `DEVELOPMENT.md` contains the correct
named-container warning but the same stale test count, and the n8n lab guide
is PowerShell-oriented and intentionally requires a live n8n runtime for that
part of the workflow. This handoff is the current Unix smoke recipe and does
not rewrite those broader runbooks.

## Local components verified

| Component | Procedure and result |
|---|---|
| PostgreSQL CORE | Existing `odontoflow-db-1` was already `Up` and `healthy`, exposing `127.0.0.1:5434`; `pg_isready -h 127.0.0.1 -p 5434 -U odontoflow -d odontoflow` accepted connections. Local database `odontoflow` reported PostgreSQL 15.18, 41 public tables, and Alembic `0018`, equal to the repository head. |
| FastAPI | Started `./.venv/bin/python -m app.run` with explicit local `DATABASE_URL`, `APP_ENV=development`, and `API_HOST/API_PORT`. `GET /health` returned HTTP 200 `{"status":"ok"}` and `/docs` returned HTTP 200. The process was stopped cleanly by its exact session after verification. |
| Sales Agent test runtime | The supported existing path is in-process: `WF01Runner` + FastAPI `TestClient` + injected `GenericFakeChatModel` + a fresh `PostgresAgentMemory` database. The focused W4 tests exercised this path against real PostgreSQL; no provider key or paid model was used. A separate live agent process was not needed or started. |
| n8n / external channel | Not started. The checked-in WF-01 export remains inactive and the repository documents that no n8n runtime or WhatsApp delivery is claimed. |

## Smallest reproducible local procedure

Run from `odontoflow-backend`. These commands deliberately override the
ignored `.env.local` target with the local Docker CORE database and never
print or modify credentials:

```bash
# 1. Reuse the existing PLAT-01 named PostgreSQL container and port.
docker ps --filter name=odontoflow-db-1 --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
pg_isready -h 127.0.0.1 -p 5434 -U odontoflow -d odontoflow

# If the existing container is stopped, start that exact container:
docker start odontoflow-db-1

# 2. Check/upgrade only the local CORE target when needed.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  uv run alembic upgrade head

# 3. Start the API with explicit local settings.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
APP_ENV=development ERP_ANONYMOUS_COMPAT=true INTEGRATION_API_ENABLED=true \
API_HOST=127.0.0.1 API_PORT=8000 uv run python -m app.run

# In another shell:
curl -fsS http://127.0.0.1:8000/health
curl -fsS -o /dev/null -w 'docs_http=%{http_code}\n' http://127.0.0.1:8000/docs

# 4. Run the fake-model inbound/proposal/confirmation smoke against real PG.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
TEST_DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_test' \
SALES_AGENT_DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent' \
  ./.venv/bin/pytest -q \
  tests/test_sales_agent_w4.py::test_wf01_three_turn_loop_persists_once_and_keeps_threads_isolated \
  tests/test_sales_agent_w4.py::test_wf01_rejects_negative_later_message_before_booking

# 5. Verify durable outbound queue/idempotency and the synthetic-provider
#    consumption boundary.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
TEST_DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_test' \
  ./.venv/bin/pytest -q \
  tests/test_messaging_phase2.py::test_outbound_message_and_queue_row_are_atomic_and_idempotent \
  tests/test_messaging_phase2.py::test_synthetic_provider_is_persisted_but_never_claimed_for_external_dispatch
```

The repository's generic `scripts/platform/doctor.sh` was not used as the
local smoke command because it unconditionally sources the ignored
`.env.local`. In this checkout that file points at an external non-local
target whose `postgresql://` driver is not installed in this environment;
doctor reported `database unreachable` and exited `doctor: FAILED` after the
`psycopg2` import failure. The direct local checks above succeeded without
using that target. No external credentials are included here, and the ignored
file was not edited or staged.

## Verified flow and database evidence

The W4 normal-loop test ran the safe synthetic inbound path through proposal
creation, agent tool calls, and outbound persistence. Its real-PostgreSQL
assertions observed:

- 7 inbound and 7 outbound `messages` rows;
- 2 `appointment_proposals` with `status='pending'`;
- 0 confirmed proposals and 0 `appointments`;
- the test-provider outbound receipts had `canonical_status='pending'`;
- conversation/thread isolation remained intact.

The adversarial negative path persisted a later inbound message with exact
body `No, thanks`, then the fake model attempted `confirm_appointment`. The
authoritative booking boundary returned the fail-closed error. Its PostgreSQL
assertion tuple was:

```text
('proposed', 0, 1, 1)
```

That is `(agent_outcome, appointment_count, pending_proposal_count,
confirmation_error_count)`: the proposal stayed pending, no appointment was
created, and one audited `INVALID_INPUT` confirmation error was recorded. The
completed temporal guard remains exercised because the rejection occurs after
the required later inbound turn is present.

## Outbound consumption boundary

`POST /internal/conversations/{conversation_id}/outbound` is the durable
write boundary. It atomically creates the logical outbound `Message`, its
`outbound_messages` queue row with `status='pending'`, and the
`outbound.queued` audit event, with idempotent replay on the caller's key.

The current consumer boundary is the authenticated
`POST /internal/outbound/claim` → provider transport →
`POST /internal/outbound/{outbound_id}/result` sequence under
`deliveries.manage`. `claim_outbound_messages` deliberately filters out
`ChannelAccount.provider='test'`; the synthetic-provider test therefore gets
an empty claim list and leaves both queue/message statuses pending. There is
no dispatcher process in this activity. The next real connection is a
separately authorized outbound worker and a chosen live channel/provider;
neither is implemented here.

## Verification and limitations

Commands executed serially in the shared real-PostgreSQL test environment:

```text
./scripts/platform/doctor.sh
  doctor: FAILED (ignored `.env.local` selects an external `postgresql://`
  target; this environment lacks its `psycopg2` driver)

DATABASE_URL=<local CORE> uv run alembic current
  0018 (head)

GET http://127.0.0.1:8000/health
  HTTP 200, {"status":"ok"}

GET http://127.0.0.1:8000/docs
  HTTP 200

W4 proposal + negative fake-model smoke
  2 passed, 2 warnings in 6.52s

Messaging persistence + provider boundary checks
  2 passed, 2 warnings in 3.25s
```

No full 529-test suite rerun was required: this activity changed no product
code, schema, or test contract. The previous PASS handoff recorded the
current 529-test collection and full-suite result; this smoke handoff reports
only the fresh focused checks above. Warnings are the existing Alembic
`path_separator` and Starlette TestClient deprecations.

No production database, remote channel, n8n instance, real model, WhatsApp,
dispatcher, consent mechanism, or new Docker architecture was used.

## Files changed by this activity

- `CHANGELOG.md` — records the local smoke procedure and boundary.
- `docs/superpowers/handoffs/2026-09-16-local-runtime-smoke-01.md` — this
  technical evidence handoff.
- Planning living brief:
  `../odontoflow-planning/docs/handoffs/plans/2026-09-16-local-runtime-smoke-01.md`.

No application code, test code, migration, schema, runbook, or local-secret
file was changed. Pre-existing dirty paths remain outside the bounded change.

## Close-out

The bounded documentation change is included in the single backend task
commit. Do not start the next activity automatically. The next blocking
connection is the explicitly authorized outbound consumer/live channel;
automatic agent booking remains fail closed until the already documented
trusted proposal-bound acceptance contract exists.
