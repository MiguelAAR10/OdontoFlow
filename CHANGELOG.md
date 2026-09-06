# OdontoFlow Changelog

## FE3A — Service-to-Cash V1 backend (2026-09-06)

- Added patient-aware charge and execution projections, charge filters, the
  uncharged-execution queue, appointment attendance uniqueness, and regenerated
  OpenAPI contracts.
- Added typed payment method codes with digital reconciliation metadata,
  organization-scoped operation-reference uniqueness, one-way verification,
  and `payments.manage` authorization.
- Added deterministic charge collection follow-ups with clinic-local promised
  dates, idempotent open/reschedule/close commands, active-debt filtering, and
  atomic settlement closure from the existing payment authority.
- Added additive migrations `0016`, `0017`, and `0018`; historical digital
  payments remain readable through the intentionally `NOT VALID` reference
  check.

## W4 — WF-01 Sales Agent V0 synthetic n8n loop (2026-09-05)

- Added the versioned, inactive `WF-01` n8n export for synthetic `provider=test`
  ingress, strict normalization, bounded conversation-scoped debounce,
  authenticated HTTP orchestration, canonical outbound persistence and a
  disabled scheduled-follow-up shape.
- Added a repository-backed HTTP contract harness and real PostgreSQL E2E test
  covering three-turn proposal/confirmation, provider-message dedupe,
  conversation/thread isolation, canonical slot selection, outbound persistence
  and handoff blocking without an n8n runtime or live provider.
- Narrowed the n8n lab agent credential to the approved `sales-agent-v0`
  permission profile.

## W3 — Sales Agent runtime (2026-09-05)

- Added the optional top-level `sales_agent` process with LangChain
  `create_agent`, structured responses, bounded execution and content-free
  per-turn telemetry.
- Added synchronous LangGraph `PostgresSaver` working memory on the separate
  `odontoflow_agent` database, with `thread_id` bound to `conversation_id` and
  explicit setup.
- Added exactly seven conversation-bound typed tools that call the
  authenticated `/agent-tools/call` gateway, plus `POST /sales-agent/turn` and
  deterministic fake-model coverage; the canonical `app` remains free of
  LangChain/LangGraph imports.

## W2 — Conversation listing and close transition (2026-09-05)

- Added authenticated, tenant-scoped `GET /internal/conversations` with status,
  exclusive `last_message_before`, deterministic ordering and a bounded limit.
- Added PF4-idempotent `POST /internal/conversations/{conversation_id}/close`
  under `conversations.manage`, with atomic audit and deterministic repeat/error
  behavior; closing releases the existing one-open-conversation contact index.
- Regenerated the committed OpenAPI documents without adding a migration.

## Sales Agent V0 integration boundary (2026-09-05)

- Kept authentication mandatory on `/internal/*` and `/agent-tools/*`, while
  making the temporary ERP anonymous compatibility mode explicit via
  `ERP_ANONYMOUS_COMPAT` (on by default in development, fail-closed in
  production).
- Removed promotions and unverified price/currency fields from reception
  context without changing the immutable migration chain.
- Added the least-privilege `sales-agent-v0` credential profile and tenant-
  scoped message redaction requiring an explicit `--organization-id`.
- Documented the canonical synthetic-data and dormant-capability boundary in
  `CANONICAL.md`.

## n8n pilot conversation context (2026-08-30)

- Extended `get_reception_context` with contact-scoped conversation state,
  profile, recent messages, confirmed appointments and the latest valid pending
  booking, cancellation or reschedule proposal.
- Kept every lookup organization- and conversation-bound; no cross-contact
  records or internal organization identifiers are exposed to the agent.
- Enforced message-retention expiry at read time, limited appointment context
  to upcoming confirmed visits and bound every pending-action lookup to the
  authenticated conversation contact.
- This makes later messages such as “la primera opción” and “confirmo la
  cancelación” recoverable from OdontoFlow instead of LLM memory.
- Focused reception and bootstrap suites: **9 passed**, 2 warnings,
  0 failures. Full PostgreSQL suite: **466 passed**, 21 warnings, 0 failures.

## n8n reception hardening (2026-08-30)

- Added an explicit synthetic `test` provider whose messages are persisted but
  whose outbounds can never be claimed for external delivery.
- Blocked all LLM-facing tools during human handoff and moved automation resume
  to an operator-only authenticated endpoint and permission profile.
- Replaced direct cancellation with a durable two-message
  proposal/confirmation contract tied to the contact and conversation.
- Added migration `0015`, regenerated OpenAPI, provisioned the synthetic clinic
  lab and added a secret-safe n8n bootstrap plus integration guide.
- Full PostgreSQL suite: **466 passed**, 21 warnings, 0 failures.

## Reception foundation port (2026-08-30)

- Ported the previously verified local integration foundation onto clean GitHub
  `main` in `codex/reception-pilot`, preserving the original dirty worktree as
  read-only evidence.
- Added migrations `0010` through `0014`: HTTP security telemetry, durable
  messaging/outbound delivery, the typed agent-tool gateway, contact-bound
  booking proposals and operational reception tools.
- Added authenticated inbound/outbound messaging, PostgreSQL-backed retries,
  contact-bound reads and mutations, proposal/confirmation booking and
  rescheduling, reception context/profile tools, promotions and human handoff.
- Imported tests before implementation; collection initially failed because
  `app.messaging` and `Promotion` did not exist. After the port, the focused
  reception/security pack passes 55 tests.
- This foundation is not yet the n8n readiness gate: provider `test`, strict
  handoff blocking, operator-only resume and two-message cancellation are
  completed in the following hardening task.

## PF5 — HTTP Authentication (2026-08-20)

- **The transport now proves who it is.** `resolve_http_context` returned
  constants, so every anonymous request resolved to the seeded `system`
  principal — which migration `0003` grants the whole 33-permission catalog.
  Measured before the change: `GET /services|/locations|/patients|/leads|
  /products|/appointments` all answered `200` with no credential, and mutations
  answered `422` (body validation) rather than `401`.
- Migration `0009`: `integration_credentials` — revocable secrets bound to a
  principal inside one organization. Only the SHA-256 digest of a 256-bit
  random secret is stored; the clear-text `prefix` is a lookup handle, not a
  secret. A composite FK into `memberships(organization_id, principal_id)`
  means a credential can never name a principal that is not already a member:
  it proves *who*, never *what*.
- `app/iam/credentials.py`: token issue/parse/verify. Every rejection path —
  absent, malformed, unknown, revoked, expired, inactive principal — returns
  the identical `401 AUTHENTICATION_REQUIRED` envelope, so responses cannot be
  used to enumerate valid prefixes. Raised with an explicit `http_status`, so
  the approved six-code envelope in `app/errors.py` is untouched (same pattern
  `PERMISSION_DENIED`/403 already uses).
- **One gate, applied at the router level.** `require_authenticated_context` is
  a dependency on all seven business routers in `create_app`; `/health` stays
  open for monitoring. This closed a second hole: `GET /services`,
  `GET /leads/{id}`, `GET /practitioners/eligible` and `POST /slots/query`
  resolved no context at all — they were unauthenticated *and* unauthorized,
  and being the first four tools the agent plan exposes, they also silently
  read organization 1 for every caller. They now pass the authenticated
  organization down, which the new cross-tenant test proves.
- Authentication uses its **own short-lived session**, never the request
  session: invariant 4 forbids pre-transaction queries on the session a service
  will `session.begin()` on.
- `scripts/issue_credential.py`: issue, list and revoke. The token is printed
  once and is unrecoverable by design.
- `tests/test_authentication.py`: 19 negative tests. Reverting the feature turns
  them red — a suite that only walks the happy path cannot detect an
  authentication regression.
- Full suite: **403 passed** (was 384).

## M4.2 — Location-Aware Inventory (2026-08-16)

- Migration `0008`: `inventory_movements` gains `location_id` (NOT NULL,
  composite FK into `locations(organization_id, id)`) — the ledger stays the
  only stock authority; the balance is still derived, now per
  Product × Location. Backfill derives consumption-linked SALIDA locations
  from their visit chain and **refuses to fabricate** locations for org-level
  rows (no such rows exist in any environment; the guard is explicit).
- Transfers: `TRANSFER_OUT` / `TRANSFER_IN` movement pair sharing a
  server-generated `transfer_id`, written in ONE transaction with the PF4
  claim, stock floor check and audit; exactly-one-Out/In per transfer and the
  pairing invariants are enforced by partial unique indexes plus a deferred
  constraint trigger (a partial or inconsistent pair cannot commit).
- `POST /products/{id}/transfers` (201, PF4-idempotent, `movements.create`
  permission). Entries/adjustments now require `location_id` in the body;
  balance and kardex take a required `?location_id=` query parameter.
- `create_service_consumption` stock-out uses the Location of its
  execution's Visit (never client-supplied); other locations are unaffected.
- OpenAPI regenerated; full suite 384 passed (was 364); evidence in
  `.audit/m4-pilot-fit/inventory-backend.md`.

## PF4 — Idempotent Commands (2026-08-15)

- Added `command_receipts` table (migration `0004`): durable exactly-once
  execution for `appointments.book`, `appointments.reschedule` and
  `appointments.cancel`, keyed by `(organization_id, operation,
  idempotency_key)` with a canonical request fingerprint.
- Added `app/idempotency/` — the application-level command handler:
  claim-first ordering inside the existing service transactions, replay of
  the stored logical outcome on identical retries, deterministic
  `IDEMPOTENCY_KEY_REUSED` (409) on fingerprint/principal mismatch.
- Transport reads the optional `Idempotency-Key` header and signals replays
  with the non-authoritative `Idempotent-Replay: true` header.
- Agents and integrations must supply an idempotency key (`INVALID_INPUT`
  422); humans keep the previous contract; absent key writes no receipt.
- The practitioner-global GiST exclusion and the existing `23P01`/`40P01`
  behaviour are unchanged.

## Accelerated Core Sprint — Agenda Integration (2026-08-15)

- Added agenda read endpoints: `GET /appointments` (half-open date window,
  location/practitioner filters, joined display names), `GET /appointments/{id}`,
  `GET /leads` (search), `GET /locations` — all org-scoped and permission-checked
  (`appointments.read`, `leads.read`, `locations.read`), OpenAPI regenerated.
- Frontend (separate repo) wired the Agenda to these endpoints with
  OpenAPI-generated types and `Idempotency-Key` on booking/reschedule/cancel;
  E2E proven against real FastAPI + PostgreSQL with no mock data.
- Mechanical path fix: `../medistock` → `../../AI-EdgeRunners/medistock` in
  engineering docs after the workspace reorganisation.

## Clinical Core — PF5 (2026-08-15)

- Added `app/clinical/` (migration 0005): `Patient` (org-owned, per-org DNI
  partial unique), `Visit` (attended encounter; optional confirmed-appointment
  origin with derived practitioner/location, or walk-in), `ServiceExecution`
  (per-visit executed services, `UNIQUE(org, visit, service)`, point-in-time
  `executed_price` snapshot).
- Six new permission codes (`patients.*`, `visits.*`, `executions.*`) seeded
  and granted to every `system` role.
- All three clinical creates are PF4-idempotent; audit provenance atomic per
  mutation (PF3); composite FKs make cross-tenant states structurally
  impossible (PF1).
- Shared PF4 claim/settle helpers extracted into `app/idempotency/service.py`
  (scheduling refactored onto them).

## Economic & Operations Bridge — PF6 (2026-08-15)

- Added `app/economics/` (migration 0006): `Product` (org-owned catalog,
  declared kind consumible/reventa, no stock authority), `ServiceConsumption`
  (execution-anchored, `UNIQUE(org, execution, product)`, quantity/price
  snapshot), `Charge` (1:1 per execution, amount from the execution price
  snapshot), `Payment` (N:1, derived paid/outstanding, deterministic
  overpayment rejection via charge row lock).
- Eight new permission codes seeded and granted to every `system` role.
- PF4 claim-first idempotency on all four creates; PF3 audit atomic.
- PF gap fix: `create_organization` provisions system access atomically
  (PR7) — runtime organizations are immediately operable.
- Frontend (separate repo): Patients screen integrated with the real clinical
  API (list/create via OpenAPI types, loading/error states).

## Inventory Ledger + PF Closure — PF7 (2026-08-16)

- Added `app/inventory/` (migration 0007): append-only `inventory_movements`
  ledger (ENTRADA/SALIDA/ADJUSTMENT with per-type CHECKs; reason-required
  adjustments) and the derived read-time `InventoryBalance` — no stock column,
  no trigger cache, one authoritative mutation path.
- Consumption now emits its SALIDA movement in the same transaction (1:1 via
  `id_consumo_origen UNIQUE` + a DB trigger enforcing product causality), with
  the negative-balance guard (product row lock + ledger sum) proven under
  concurrency.
- New endpoints: `POST /products/{id}/entries`, `POST /products/{id}/adjustments`,
  `GET /products/{id}/movements`, `GET /products/{id}/balance` (PF4-idempotent
  creates, `movements.read/create` permissions).
- PF closure: every remaining mutating service (lead, service, location,
  practitioner, membership, capability, availability rule/block) is now
  ctx-gated permission-checked; BLOCKER-2 resolved (lead creation is
  org-wide-only, E5).
