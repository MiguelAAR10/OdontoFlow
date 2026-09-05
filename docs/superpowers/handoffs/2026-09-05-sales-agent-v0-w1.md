# W1 Handoff — Sales Agent V0 integration boundary

Date: 2026-09-05
Workstream: W1 — canonical integration plus forward fixes
Owner: GPT-5.6 Luna Max via Orca
Status: implementation and verification complete; this document is part of the single forward-fix commit

## Objective

Create the dedicated `integration/sales-agent-v0` branch from canonical `main`,
merge the n8n reception candidate without rewriting contributor history, and
land one forward-fix commit that narrows integration authentication, restores a
flagged ERP compatibility seam, removes unverified commercial data from agent
context, issues a least-privilege V0 credential, makes redaction tenant-safe,
and records the canonical-data boundary.

W1 deliberately does not implement `sales_agent`, LangChain, n8n workflows,
W2–W6, migrations, frontend changes, infrastructure, or any protected file.

## Result and lifecycle

- Branch created: `integration/sales-agent-v0` from `main` at `0ddd3a3`.
- Candidate merged with `git merge --no-ff`.
- Merge commit: `4cba2e37c8b5d247c6d00d7ed573bf248694ddad`.
- Forward-fix commit: one commit with subject
  `fix: scope sales agent v0 integration boundaries` (the commit containing
  this handoff; its final SHA is reported by the worker after commit).
- No squash, rebase, amend, reset, force-push, migration edit, or canonical
  business database migration was performed.

### Preserved contributor history

All four Leonardo commits remain individually reachable and authored by
`leonardopanduro-rgb`:

1. `1f2d8c1` — require an authenticated credential on every business route
2. `006a76d` — port verified reception foundation
3. `c45971e` — harden reception gateway for n8n
4. `d5274d3` — expose contact-bound reception context

The merge parents are `0ddd3a3` and `d5274d3`; no contributor commit was
rewritten.

## Changed files and reasons

- `app/__init__.py` — apply the bearer gate only to `/agent-tools/*` and
  `/internal/*`; preserve ERP router order.
- `app/config.py` — add `ERP_ANONYMOUS_COMPAT`, defaulting on in development,
  off in production, and reject an enabled production value.
- `app/context.py` — use the explicit ERP fallback only when the flag permits
  it, preserve middleware trace IDs, and fail closed with 401 when disabled.
- `app/agent_tools/reception.py` — remove promotion queries and
  `base_price`/`currency` from reception context; leave migrations/models
  untouched.
- `app/agent_tools/service.py` — render authorization denials as stable HTTP
  403 envelopes after recording the tool audit event.
- `scripts/issue_credential.py` — add exact `sales-agent-v0` permission profile.
- `app/messaging/service.py` — require `organization_id` and constrain both
  redaction selection and update to that tenant.
- `scripts/redact_message_content.py` — require a positive explicit
  `--organization-id`; no implicit all-tenant default remains.
- `tests/test_authentication.py` — retarget authentication and cross-tenant
  proofs from ERP `/services` probes to integration routes.
- `tests/test_security_boundary.py` — prove scoped dependencies, OpenAPI
  security scope, compatibility flag behavior, profile permissions, and 403s.
- `tests/test_reception_agent_phase5.py` — prove commercial fields are absent
  and V0 can propose/confirm booking while dormant capabilities are forbidden.
- `tests/test_messaging_phase2.py` — prove tenant-scoped redaction and CLI
  fail-closed behavior.
- `docs/api/openapi.json` and `docs/api/openapi.yaml` — regenerated from the
  factory; only ERP bearer `security` declarations changed.
- `CANONICAL.md` — document synthetic/unverified prices and promotions,
  OdontoSmart evidence boundary, consent-status limitation, and dormant
  reception capabilities.
- `CHANGELOG.md` — record the shipped W1 boundary changes.

No `alembic/versions/*`, `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py`, existing ERP schemas, MediStock,
ODONTO-SMART, or odontoflow-sim files changed.

## Auth before and after

Before the forward fix, the merged candidate applied
`require_authenticated_context` to every business router, including ERP
routes; anonymous `/services` calls returned 401.

After the forward fix:

- `/agent-tools/*` and `/internal/*` remain credential-gated and rate-limited.
  Missing, malformed, revoked, expired, inactive, and unknown credentials
  return the same 401 envelope.
- ERP routes use explicit `ERP_ANONYMOUS_COMPAT`: default `true` in
  development preserves the seeded compatibility context and default `false`
  in production returns 401. Enabling the flag in production raises a config
  error.
- Integration credentials resolve organization and principal only from
  PostgreSQL. A credential from another organization cannot enqueue against a
  conversation it does not own; the test proves a 404 without cross-tenant
  leakage.
- A member without permissions is denied; `sales-agent-v0` receives 403 for
  cancellation, rescheduling, operator resume, delivery management, and
  unrelated profile management.

## Contracts

`get_reception_context` still returns organization, service identity/duration,
booking mode, locations, conversation/profile/messages/appointments/pending
action and safety fields. It no longer returns a `promotions` key or any
`base_price`/`currency` fields.

The exact `sales-agent-v0` profile grants only:

`conversations.read`, `services.read`, `locations.read`,
`availability.read`, `contact_appointments.read`,
`contact_appointments.book`, `conversations.manage`, `deliveries.create`.

The profile successfully proposes and confirms a deterministic booking. It
does not grant `contact_appointments.cancel`,
`contact_appointments.reschedule`, `conversations.resume`,
`deliveries.manage`, or `contact_profiles.manage`.

The redaction command contract is now:

```text
python scripts/redact_message_content.py --organization-id <positive-int> [--limit N]
```

Omitting the tenant is an argparse error before a database session is opened.

## Exact verification evidence

Risk-weighted red/green checks:

- Focused auth/context/reception/messaging pack after implementation:
  `67 passed, 2 warnings`.
- Permission-propagation red/green check:
  `3 passed, 2 warnings` for integration permission denial and the complete
  V0 booking/dormancy test.
- Full local PostgreSQL suite:
  `.venv/bin/python -m pytest -q` → `471 passed, 0 failed, 21 warnings`.
- Full suite on dedicated scratch DB
  `odontoflow_sales_agent_w1_eval`:
  `TEST_DATABASE_URL=postgresql+psycopg://.../odontoflow_sales_agent_w1_eval
  .venv/bin/python -m pytest -q` → `471 passed, 0 failed, 21 warnings`.
- The first scratch invocation failed only because the repository fixture
  auto-creates the fixed `odontoflow_test` name; the explicitly named scratch
  database was then created and the complete rerun passed.
- `git diff --check` passed.
- `python -m compileall -q app scripts tests` passed.
- `alembic heads` reports `0015 (head)`; migration tests passed and no
  migration file is in the diff.
- OpenAPI regenerated with `scripts/generate_openapi.py`: 38 paths before and
  after, no path/operation additions or removals, and no non-security
  operation changes. The 41 intentional changes remove bearer declarations
  from ERP operations while retaining them on integration operations.

## Migration and OpenAPI evidence

The linear `0009` through `0015` chain is preserved exactly. Promotions,
proposal tables and pricing columns remain physically present under the
approved containment boundary; the agent cannot reach promotions/pricing, and
booking behavior does not read promotions.

The generated OpenAPI documents contain 38 paths. `/agent-tools/*` and
`/internal/*` declare `IntegrationBearer`; ERP paths do not claim a bearer
requirement while anonymous compatibility is on. `/health` remains public.

## Retained security debt and open question

ERP business routes can still resolve to the seeded `system` principal with all
permissions while `ERP_ANONYMOUS_COMPAT=true`. This is a temporary development
compatibility trade, not production-safe behavior. Owner: Miguel Arias.
Remove it when the first of these occurs: ERP frontend credential flow ships,
backend is exposed beyond localhost/pilot, or real clinic data is loaded.

The provenance of the five seeded prices remains an open clinic question.
Until confirmed, all seeded prices/promotions are explicitly
**UNVERIFIED / NON-AUTHORITATIVE** and no commercial claim or upsell may rely
on them. `consent_status` is a declared field, not proof of compliance or
lawful consent.

## Deferred capabilities

W1 leaves cancellation, rescheduling, outbound claim/settle dispatcher,
lab seeder, template messages, consent wiring, agent-tool rate-bucket
reclassification, and all LangChain/sales-agent runtime work deferred to the
approved later verticals and triggers. No W2–W6 code was started.

## Blockers

None for W1 acceptance. The price provenance question is recorded as an open
product/compliance question but does not block deterministic V0 booking because
the data is excluded from agent context.

## Rollback

Before moving `main`, discard only the integration branch if needed; `main`
remains untouched. After publication, use forward reverts (first the single
forward-fix commit, then `git revert -m 1 4cba2e3` if the merged candidate must
also be removed). Never reset or force-push. Do not downgrade the migration
chain after real conversation data exists. The scratch database is disposable;
dropping it cannot affect canonical business state.

## Next activity

Fast-forward `main` only after coordinator review of this diff and the exact
verification evidence. The next approved workstream is W2: conversation list
and close transition; W3–W6 remain out of scope for this handoff.
