# SANDBOX-OUTBOUND-02 — Technical handoff

Date: 2026-09-16

Base: `ad72435022054241fa27a784615b9ba8062ffecc`

Status: **PASS — first-class local sandbox delivery verified; no next activity
started**

## Result

The approved development-only `sandbox` provider now has a bounded,
authenticated local delivery path:

```text
persisted outbound (provider=sandbox, pending)
  → deliveries.manage claim(provider=sandbox)
  → authenticated loopback /internal/sandbox/receive
  → one durable exact-payload receipt
  → existing outbound result settlement
  → delivered / failed / dead_letter state
```

`provider=test` remains persisted but non-dispatchable. The sandbox consumer
cannot claim `test` or `whatsapp` rows because the server claim is explicitly
filtered to `provider=sandbox`; the receiver also rejects non-sandbox rows.
There is no live-provider fallback, arbitrary destination URL, inbound
adapter, paid model, n8n deployment, or production write.

## Server contract

- Additive migration `0019` admits `sandbox` alongside the existing
  `whatsapp` and `test` values. Existing rows and provider behavior are
  preserved. The migration adds `sandbox_delivery_receipts` and the
  tenant-qualified unique key required for its composite outbound FK.
- `POST /internal/outbound/claim` accepts an optional provider selector. The
  service allowlist is `whatsapp` and `sandbox`; the default claim query also
  excludes `test` and any future unallowlisted value.
- `POST /internal/sandbox/receive` is on the existing authenticated messaging
  router and requires `deliveries.manage`. It accepts only the canonical
  sandbox text payload, locks the tenant-scoped outbound row, checks exact
  payload equality, and creates one immutable receipt. Replaying the same
  outbound/payload returns the same server-generated `sandbox-{outbound_id}`
  provider id without another receipt or audit mutation.
- Sandbox success settlement (`sent` or `delivered`) requires that matching
  server-owned receipt. A caller cannot manufacture sandbox delivery success
  by posting a provider id directly to the generic settle endpoint. Failure
  settlement still uses the existing retry/dead-letter contract.
- Receipt persistence is tenant-bound by PostgreSQL composite FK, and all
  service reads use the authenticated `ExecutionContext` organization. A
  caller from another organization sees `NOT_FOUND` and cannot create a
  receipt.
- `outbound-dispatcher` remains the existing least-privilege
  `deliveries.manage` profile. No new IAM vocabulary or authentication system
  was introduced. The consumer sends server-issued bearer credentials and
  stable UUIDv4 settlement idempotency keys derived per durable attempt.

## Consumer and local receiver

`integrations/sandbox/consumer.py` is a one-shot consumer, not a generic
messaging platform. `scripts/sandbox_dispatcher.py` runs one bounded poll,
delivery pass, and exit. Its settings fail closed when the dispatcher token or
receiver URL is missing. The backend and receiver URLs must be explicit
loopback origins; the receiver path must be exactly
`/internal/sandbox/receive`, with no credentials, query, fragment, or remote
host. HTTP 5xx/408/429 receiver failures settle `transient_failure`; other
receiver rejection or malformed payload failures settle
`permanent_failure`. Claim or settlement boundary failures are surfaced and
never reported as delivery success.

The receiver is the existing FastAPI process at a local authenticated route,
so the smoke requires only PostgreSQL, FastAPI, and the checked-in consumer.
The receiver stores a hash rather than a second copy of message content; the
canonical outbound JSON remains the authoritative payload.

## Reproducible local startup and smoke

Run from `odontoflow-backend`. Use only the existing local PostgreSQL CORE
container and a disposable/local database. Never use these commands against a
production database.

```bash
# Existing CORE and local migration target.
docker ps --filter name=odontoflow-db-1 --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
pg_isready -h 127.0.0.1 -p 5434 -U odontoflow -d odontoflow
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  ./.venv/bin/alembic upgrade head

# In a second shell, keep the API local and development-only.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
APP_ENV=development ERP_ANONYMOUS_COMPAT=true INTEGRATION_API_ENABLED=true \
API_HOST=127.0.0.1 API_PORT=8000 ./.venv/bin/python -m app.run

# Provision a tenant-owned sandbox channel; this has no provider secret.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  ./.venv/bin/python scripts/provision_channel_account.py \
  --organization 1 --provider sandbox --external-account-id local-sandbox \
  --display-name 'Local sandbox'

# Issue separate server credentials using the existing profiles. Each token
# is printed once; place it only in the current shell or local secret store.
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  ./.venv/bin/python scripts/issue_credential.py issue --organization 1 \
  --name sandbox-inbound --type integration --profile n8n-inbound
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  ./.venv/bin/python scripts/issue_credential.py issue --organization 1 \
  --name sandbox-agent --type integration --profile conversation-agent
DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow' \
  ./.venv/bin/python scripts/issue_credential.py issue --organization 1 \
  --name sandbox-dispatcher --type integration --profile outbound-dispatcher

# Set the last token from the command output without committing or logging it.
export SANDBOX_BACKEND_URL=http://127.0.0.1:8000
export SANDBOX_RECEIVER_URL=http://127.0.0.1:8000/internal/sandbox/receive
export SANDBOX_DISPATCHER_TOKEN='<server-issued outbound-dispatcher token>'
./.venv/bin/python scripts/sandbox_dispatcher.py --limit 10
# Expected one-shot result for one due row: claimed=1 delivered=1 failed=0
```

The inbound and conversation-agent credentials can be used with the existing
authenticated normalized inbound and outbound HTTP contracts to seed one
synthetic sandbox conversation. For a no-secret reproducible proof, run the
real-PostgreSQL regression below; it creates and cleans its own fixture rows
and exercises the same FastAPI routes and consumer with injected TestClient
transport.

## Evidence and validation

The intentional red phase came before implementation: the provider insert was
rejected by the old PostgreSQL `ck_channel_accounts_provider`; after the
provider/claim patch, the receiver/consumer tests failed because the receiver
route, receipt table and consumer module did not yet exist. The implementation
was then driven to green.

```text
./.venv/bin/python -m pytest -q tests/test_sandbox_outbound.py
10 passed, 2 warnings in 18.48s

./.venv/bin/python -m pytest -q tests/test_migrations.py
9 passed, 20 warnings in 39.43s
```

The migration test's empty-database case creates a throwaway PostgreSQL
database, upgrades it to `head`, checks the exact table set including
`sandbox_delivery_receipts`, and the migration suite also exercises
downgrade/re-upgrade cycles. The sandbox regression proves persistence,
provider-only claim, authenticated receipt creation, duplicate replay,
receipt-required success settlement, transient receiver failure and retry,
cross-tenant rejection, and that `test`/`whatsapp` remain untouched.

The final serial backend suite was:

```text
./.venv/bin/python -m pytest -q
545 passed, 21 warnings in 615.36s (0:10:15)
```

The current collection is **545**, versus the historical **527-pass** baseline
and **535** tests at this activity's intake. The 10-test increase is the new
sandbox regression file. The warnings are the existing Starlette TestClient
deprecation and Alembic `path_separator` deprecation (19 migration warnings
plus one booking warning); no failures or skips occurred.

Additional final checks passed: focused Ruff on all changed Python code,
`compileall`, OpenAPI JSON parsing, dispatcher CLI help, and `git diff --check`.
The checked-in OpenAPI JSON/YAML now includes the sandbox route and schemas.

## Files in the bounded change

- `alembic/versions/0019_sandbox_provider.py`, `alembic/env.py` — additive
  provider/receipt schema and model registration;
- `app/messaging/models.py`, `schemas.py`, `service.py`, `router.py` — provider
  selector, canonical receiver, receipt authority, and sandbox settlement
  guard;
- `integrations/sandbox/__init__.py`, `integrations/sandbox/consumer.py`,
  `scripts/sandbox_dispatcher.py` — loopback-only authorized consumer;
- `scripts/provision_channel_account.py`, `.env.example` — local provisioning
  and configuration contract;
- `tests/test_sandbox_outbound.py`, `tests/conftest.py`,
  `tests/test_migrations.py`, `tests/test_tenant_integrity.py` — real
  PostgreSQL proofs and fixture/schema expectations;
- `docs/api/openapi.json`, `docs/api/openapi.yaml`, `CHANGELOG.md` — generated
  and shipped contract documentation;
- this handoff.

Protected paths `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py`, existing migrations, and `../../medistock`
were not changed. Existing Sales Agent authentication, fail-closed booking,
tenant/audit/idempotency behavior, n8n synthetic provider, and unrelated
working-tree paths were preserved.

## Commit and close-out

The implementation, tests, migration, generated contract, changelog, and this
handoff are included in one bounded task commit. The enclosing commit SHA is
recorded in the planning CAVELOG, STATUS, and final activity response.

Planning living brief:
`../odontoflow-planning/docs/handoffs/plans/2026-09-16-sandbox-outbound-02.md`

No next activity is authorized or started automatically. `sandbox` is local
development/testing only; WhatsApp, inbound channel integration, deployment,
real model billing, patient consent changes, and automatic booking remain
outside this activity.
