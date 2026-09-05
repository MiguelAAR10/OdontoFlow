# Handoff — PF5: HTTP authentication

**Branch:** `codex/http-authentication` (from `main` @ `27bc400`)
**Scope:** close the authentication gap found by the security audit. Nothing
from `PLAN.md` (agents, n8n, WhatsApp) was implemented.

---

## 1. The defect

`app/context.py::resolve_http_context` returned constants:

```python
organization_id = BOOTSTRAP_ORGANIZATION_ID   # 1
principal_id    = SYSTEM_PRINCIPAL_ID
principal_type  = "system"
```

The `system` principal holds **33 of 33** permissions (migration `0003`). So
every anonymous HTTP request was a superuser in organization 1.

Measured on the running API before the change:

| Probe | Result |
|---|---|
| `GET /services /locations /patients /leads /products /appointments` | `200`, no credential |
| `POST /leads /appointments /services /locations` | `422`, **not** `401` |

The `422` is the proof: the request reached body validation, i.e. it passed
authentication. There was no door.

### The second defect, found while fixing the first

Four routes resolved **no context at all**, so they were neither authenticated
nor authorized, and their tenant fell back to the bootstrap default — meaning
any caller read organization 1:

- `GET /services`
- `GET /leads/{lead_id}`
- `GET /practitioners/eligible`
- `POST /slots/query`

These are exactly the first four tools `PLAN.md` Phase 3 exposes to the agent.
The earlier audit had reported "no cross-tenant leaks": the queries *are*
scoped, but the value they scope by was a constant, which only a second
organization could reveal. `test_a_credential_cannot_read_another_organization`
now covers it.

---

## 2. What changed

### New

| File | Purpose |
|---|---|
| `alembic/versions/0009_integration_credentials.py` | Additive table; nothing dropped or altered |
| `app/iam/credentials.py` | Token issue/parse/verify, model, 401 error |
| `scripts/issue_credential.py` | issue / list / revoke |
| `tests/test_authentication.py` | 19 negative tests |

### Modified

| File | Change |
|---|---|
| `app/context.py` | `require_authenticated_context` gate; `resolve_http_context` returns the cached context and never invents identity |
| `app/__init__.py` | Gate applied to the 7 business routers; `/health` left open |
| `app/catalog/router.py`, `commercial/router.py`, `organization/router.py`, `scheduling/router.py` | The 4 context-less routes now resolve and pass the tenant |
| `alembic/env.py` | Register the new model |
| `tests/conftest.py` | Seed a `human` operator principal + fixed credential |
| 13 test files | `TestClient(..., headers=AUTH_HEADERS)` + `app.state.auth_sessionmaker` |
| `tests/test_migrations.py`, `test_authorization.py`, `test_tenant_integrity.py` | Ground-state assertions updated for the new table/principal |

**Scope guard respected**: `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py` and existing migrations were not touched.
The 401 is raised with an explicit `http_status`, the pattern
`app/iam/service.py` already uses for `PERMISSION_DENIED`/403.

---

## 3. Design decisions

**One gate, at router level.** A per-endpoint call is a gate that eventually
gets forgotten — that is precisely how those four routes ended up open.
`create_app` applies `require_authenticated_context` to every business router,
so a new route is protected by default.

**Authentication runs on its own session.** Invariant 4 forbids pre-transaction
queries on the session a service will call `session.begin()` on, and booking
requires an idle Session. Sharing the request session would have broken booking
quietly. The factory is read from `app.state.auth_sessionmaker` so tests bind it
to the test engine, exactly as they already override `get_db`.

**SHA-256, not a slow KDF.** The secret is 256 random bits, not a human
password. A KDF defends low-entropy secrets and would add its cost to every
request; against this input it buys nothing.

**Indistinguishable rejections.** Unknown, revoked, expired, inactive and
malformed all return the same status, code and message, so the response cannot
confirm which prefixes exist.

**The test principal is `human`, not `integration`.** `KEY_REQUIRED_PRINCIPAL_TYPES`
already forces an `Idempotency-Key` on every mutation by an agent or
integration — the rule `PLAN.md` Phase 6 depends on. Typing the shared fixture
as an integration would have forced a key into ~150 unrelated fixtures and
diluted that guarantee instead of testing it; `test_authentication.py` creates
integration principals explicitly.

---

## 4. Bug found and fixed during implementation

`split_token` split on every `_`. `secrets.token_urlsafe` draws from the
base64url alphabet, which **includes** `_`, so roughly any token whose secret
contained an underscore failed to parse — an intermittent, ~random
authentication failure. Now `split("_", 2)`. Covered by the round-trip in
`test_the_secret_is_never_stored_in_clear` and by issuing real tokens.

---

## 5. Verification

**Suite:** `403 passed` (was 384). Zero failures.

**Live, against `127.0.0.1:8011`:**

| Probe | Before | After |
|---|---|---|
| `GET /services` (and 5 more) | `200` | `401` |
| `GET /practitioners/eligible` | `200` | `401` |
| `POST /slots/query` | `200` | `401` |
| `POST /services` | `422` | `401` |
| `GET /health` | `200` | `200` (open by design) |

**Credential lifecycle:**

```
issue  → Authorization: Bearer ofk_…  → 200
tamper → 401
revoke → 401 immediately
```

---

## 6. Risks and debt

- **The dev database was migrated to `0009`.** The test database too. No data
  was altered; the table is new and empty apart from credentials issued during
  verification (the `n8n-inbound` one was revoked).
- **No rate limiting and no payload cap.** Still open, still measured: 30/30
  requests answered `200`, and a 4.8 MB body was parsed. These are Phase S2 of
  `PLAN-SEGURIDAD.md`, not this task.
- **No credential rotation window.** Revocation is immediate; issuing a
  replacement before revoking the old one is a manual, two-step procedure.
- **`last_used_at` writes on every authenticated request.** One extra UPDATE per
  call; if it shows up under load, batch or drop it.
- **`default_context` still exists** for fixtures and scripts. No router imports
  it, but nothing enforces that yet — the structural test is Phase S4.

---

## 7. Not done

No commit was pushed. No merge. No workflow activated. Nothing from `PLAN.md`.

Suggested next step: Phase S2 of `PLAN-SEGURIDAD.md` (rate limiting, payload
cap, security headers) before any URL becomes reachable by n8n.
