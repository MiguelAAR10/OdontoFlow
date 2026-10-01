# OdontoFlow Changelog

## SELF — Patient self-booking + D-1 reminders (2026-10-01)

- `POST /public/bookings` (scheduling router, authenticated): the frontend BFF
  (new integration-only profile `patient-booking`: services/locations/
  practitioners/availability reads + `appointments.create`; `--type agent` is
  refused) books a **confirmed** appointment for the patient (L3 by the
  patient, no proposal). UUIDv4 `Idempotency-Key` per submit
  (`public_booking.create` receipt, replay → `Idempotent-Replay: true`). One
  transaction: past start → 422, per-phone advisory lock, lead resolved by
  contact identity then lead phone (else a `direct` lead), rate limit 3 per
  phone per 24 h counted by resolved lead OR phone → 429
  `PUBLIC_BOOKING_RATE_LIMITED`, given practitioner's membership/capability
  checked before the slot (404/409), off-grid/outside hours/blocked → 422,
  taken → 409 `SLOT_BLOCKED`. Response carries `reference` (`OF-<id>`); audit
  `appointment.created` + domain event `appointment.booked_by_patient` (ids
  and times only).
- `POST /agent-runs {agent_key:"confirmaciones"}`: human-only (agent/
  integration → 403) with `appointments.read` + `deliveries.create`; kill
  switch `AGENT_CONFIRMACIONES_ENABLED`. One SQL selects tomorrow's confirmed
  appointments (location timezone) with their latest reachable conversation
  (lead, patient or phone match; not opted out, not closed) and queues one
  fixed-template reminder each as the caller; key derived from
  `(org, appointment, start)` so a reschedule gets a new reminder and a rerun
  dedupes. No conversation → `skipped`. A failed run's counts are not
  authoritative (already-queued reminders stay; the rerun dedupes them).
- Migration `0024_agent_runs_confirmaciones` (head `0024`): `agent_key` adds
  `confirmaciones`. OpenAPI regenerated.
- Known limits: exact E.164 matching (staff-typed leads without `+51` are not
  matched); `POST /appointments` stays reachable by the BFF credential until a
  `public_bookings.create` code lands; without OTP anyone knowing a phone with
  an open conversation can trigger reminders to it (rate limited).

## B3 — Staff reads: chat, handoffs, activity feed, productivity, reception runs (2026-10-01)

- Human-only staff reads (agents/integrations get 403 even when they hold the
  code; they keep `/internal/*` and their tools): `GET /conversations` (keyset on
  `last_message_at DESC, id DESC`, filters `status`/`location_id`, contact
  display name patient → lead → masked phone, 80-char preview),
  `GET /conversations/{id}/messages` (oldest → newest; `text` null when redacted
  or expired, `media_reference` never returned), `GET /handoffs?status=`
  (queue, oldest first; claimant derived from the conversation assignee only
  while `claimed`) and `POST /handoffs/{id}/claim` (UUIDv4 `Idempotency-Key`,
  `conversations.resume`, audit `reception_handoff.claimed`, 409
  `HANDOFF_NOT_PENDING`). Hand-back reuses `/internal/conversations/{id}/resume`.
- `app/observability/`: `GET /activity` (`proposals.read`) = one SQL `UNION ALL`
  of audit rows, proposal audit rows (agent + appointment proposals, with
  `agent_key`/`location_id`) and `agent_runs`; `actor_kind` + display name
  ("Sistema" for system); `summary` from a closed Spanish template map, never
  from state JSON. `GET /metrics/productivity?from&to&location_id`
  (`audit.read`, ≤ 92 days, local dates): appointments completed/no-show/
  cancelled, charged/collected/outstanding (net of reversals), proposals per
  agent with effective (lazy) expiry and reception declines from audit,
  collection reminders approved. Computed on the fly, no stored aggregates.
- Migration `0023_agent_runs_reception` (head `0023`): `agent_key` adds
  `reception`; `conversation_id` + `trigger_message_id` with a composite FK into
  `messages(organization_id, conversation_id, id)`; reception rows must be
  `event` with both set. `sales_agent/api.py` records one run per turn on every
  path (completed / failed with category / 503 build failure), best effort: a
  persistence failure never changes the turn response. No audit row per turn.
- Profiles: secretaria `+conversations.resume`; administrador `+audit.read`
  (productivity gate — `audit.read` means clinic productivity reads, not the
  feed). `AgentRunOut.agent_key` now `cobranza|reception` (output only).

## MCPCLI — Agentic doors: `odontoflow` CLI + MCP server over the catalog (2026-10-01)

- New HTTP-only packages (no `app`/SQLAlchemy/psycopg import; a test enforces
  it): `odontoflow_cli/` (`OdontoflowApi` shared client + argparse CLI:
  `tools list`, `call`, `inbox`, `approve`, `decline`, `runs start cobranza`,
  `me`; `--json`; exit codes 0/1/2/3) and `odontoflow_mcp/` (`build_server`,
  official `mcp` SDK 2.2). Config via `ODONTOFLOW_TOKEN` / `ODONTOFLOW_URL`.
- The MCP server lists `GET /agent-tools/catalog` for the process token (minus
  L4) as proxy tools to `POST /agent-tools/call`, plus `odontoflow_inbox`,
  `odontoflow_start_cobranza_run` and `odontoflow_me`. It never exposes
  approve/decline and refuses a human token at startup (`HUMAN_TOKEN_REFUSED`).
  Mutation keys are stable per (conversation, tool, args), so a retry replays.
  stdio is the default; streamable-http is opt-in, loopback only, untested.
- The only client-side policies are the L4 display filter and no MCP approval.
  Everything else stays server-side (an agent `call confirm_appointment` still
  gets the server's 403).
- `pyproject.toml`: extra `mcp` (`httpx`, `mcp>=2.2,<3`), `mcp` in the dev
  group, console scripts `odontoflow` and `odontoflow-mcp`; `uv.lock` updated.
  No route, table, permission or migration change (OpenAPI unchanged).
- Known debt, documented in `docs/agents/mcp-cli.md`: `airy-cobranza` falls
  back to the reception allowlist (B4), so its catalog lists 13 tools that all
  return 403 for it.

## COB — Collections agent: deterministic sweep, "run now", reminder proposals (2026-10-01)

- Migration `0022_agent_runs` (head `0022`): table `agent_runs` (closed
  `agent_key`/`trigger`/`status` CHECKs, running ⇔ no `finished_at`, failed ⇔
  `error_category`, completed ⇒ proposed+deduped+skipped = candidates,
  `triggered_by` composite FK into `memberships`). No new permission codes.
- `app/agents_runtime/`: `POST /agent-runs {agent_key:"cobranza"}` runs the
  sweep synchronously. One org-scoped SQL selects charges with a net-of-reversals
  balance and local age ≥ 7 days; a fixed Spanish template drafts the reminder
  (no LLM); each candidate becomes at most one `collection_reminder` proposal
  (`agent_key='cobranza'`, evidence with amount, balance, `days_since_issued`,
  last payment). A charge already proposed today counts as `deduped`; no
  reachable conversation / paid meanwhile counts as `skipped`; any other error
  fails the run. The agent never approves or executes. `GET /agent-runs`
  (`proposals.read`) lists runs newest first.
- Receipt settles in tx1: a same-key replay returns the run as it is now
  (even `failed`) and never sweeps again; retry a failed run with a new key.
- Kill switch `AGENT_COBRANZA_ENABLED=false` → 409 `AGENT_DISABLED`
  (`reason=disabled`); a human trigger without an active, permitted
  `airy-cobranza` member in the org → 409 `reason=not_provisioned`.
- **Deviation from the card:** the human trigger gate is `proposals.decide` +
  `charges.read`, so **secretaria can trigger runs too**, not only the admin.
  Kept on purpose: a run only proposes, approval stays human. Admin-only needs
  a new `agent_runs.execute` code (follow-up).
- `create_proposal` gains a server-only `agent_key` keyword (never from a body).
- `scripts/seed_demo.py`: a sandbox conversation for the S/ 180 patient only and
  the `airy-cobranza` agent principal (`collections-agent`, no credential).
- Regenerated `docs/api/openapi.json`/`openapi.yaml` (2 new routes, additive).

## B2 — Agent proposals, approval inbox, approve/decline (2026-10-01)

- Migration `0021_agent_proposals` (head `0021`): table `agent_proposals`
  (closed kind/status CHECKs, decider/result/error coherence, tenant composite
  FKs incl. proposer/decider via `memberships`, unique `execution_key`,
  partial unique `uq_agent_proposals_open_dedupe` on pending/approved) and the
  permissions `proposals.read`, `proposals.create`, `proposals.decide` (56).
- `app/proposals/`: `kind → executor` registry with `collection_reminder`
  (queues a WhatsApp message on the patient's reachable conversation; opted-out
  contacts are unreachable) and `collection_follow_up` (`open_follow_up`).
  Hash, subject version, TTL (72 h), location and dedupe key are server-computed;
  `agent_key` is derived from the principal, never from the body.
- Routes: `POST /agent/proposals` (agent/integration, `Idempotency-Key`
  required; 201 new / 200 dedupe), `GET /agent/inbox` (agent + appointment
  proposals in one keyset-paged list), `GET /agent/proposals/{id}`,
  `POST /agent/proposals/{id}/approve` (human only, `Idempotency-Key`; exactly
  one execution under the human's context) and `/decline` (idempotent).
- New error codes outside `app/errors.py` (IamErrorCode pattern):
  `PROPOSAL_HASH_MISMATCH` 409, `PROPOSAL_SUPERSEDED` 409,
  `PROPOSAL_NOT_PENDING` 409, `PROPOSAL_EXPIRED` 410.
- Profiles: `secretaria` += `proposals.read`, `proposals.decide`,
  `deliveries.create` (`administrador` inherits); new agent profile
  `collections-agent` (propose only; never `proposals.decide`).
- Regenerated `docs/api/openapi.json`/`openapi.yaml` (5 new routes, additive).

## IDN — Human identity per person, secretaria/administrador, GET /me (2026-10-01)

- `scripts/issue_credential.py`: `--type human` is now issuable, only with the
  new human profiles `secretaria` and `administrador` (`HUMAN_PROFILE_PERMISSIONS`,
  explicit tuples, no new codes, head stays `0020`). A mismatched type/profile
  (human + integration profile, integration/agent + human profile) or `system`
  exits 2, so an agent never holds `payments.reverse`. Human roles are
  `staff-secretaria` / `staff-administrador`; integration roles keep
  `integration-<profile>`. A human principal is never reused across
  organizations by name (one person per clinic).
- `secretaria`: agenda, patients, visits, the till (charges, payments incl.
  `payments.manage`, follow-ups), waitlist, `conversations.read` and
  `contact_appointments.book` (approve appointment proposals).
  `administrador` adds products, movements (entries/transfers),
  `reorder_points.manage` and `payments.reverse`.
- New `GET /me` (authenticated, read-only): principal `{id, type, display_name}`,
  organization, `roles [{code, name}]` of the credential's organization only
  (active membership), and sorted `permissions`. It counts against the read
  rate limit; the BFF should cache it per session.
- `seed_demo.py --issue-staff-credential` also issues Lucía Ramos (secretaria)
  and Carlos Vega (administrador) in the same transaction and writes
  `BACKEND_DEMO_HUMANS` (JSON `[{role, display_name, token}]`) as line 2 of
  `.env.demo.local` (0600). All credentials commit once before the single file
  write. `reception-staff-demo` is unchanged.
- Human mutations keep an optional `Idempotency-Key`; the BFF must keep sending
  it so retries replay.
- Regenerated `docs/api/openapi.json`/`openapi.yaml` (1 new route, additive).

## B1 — ToolSpec registry, server allowlist, stable idempotency key, catalog (2026-10-01)

- `app/agent_tools/registry.py` is the single source for the 16 agent tools:
  each `ToolSpec` declares its args model, handler, effect
  (`read|propose|execute`), level (`L0..L4`), documented permissions,
  `needs_conversation` and a description. `READ_TOOL_NAMES`,
  `MUTATION_TOOL_NAMES` and `ARGUMENT_MODELS` are derived from it, and
  `call_agent_tool` dispatches through the registry (the if/elif is gone).
- Server-side allowlist per `agent_key` (config in code, no migration):
  `reception` = the 16 tools minus the L4 `confirm_*` tools. Agent principals
  never reach an L4 tool, and a tool outside their allowlist gets HTTP 403
  `PERMISSION_DENIED` plus a `security_events` row (`agent_tool_denied`,
  `blocked`). The gate runs before trace validation. Human, integration and
  system principals are still governed by permissions only.
- `AgentToolCall.conversation_id` is now optional; a tool that needs a
  conversation returns `INVALID_INPUT` without one. The `agent_tool.called`
  audit adds `agent_key`, and `entity_id` falls back to `"none"`.
- New `GET /agent-tools/catalog` (authenticated, read-only). An agent sees its
  allowlist (never L4); other principals see all 16 tools with their JSON
  argument schemas.
- Sales Agent gateway: mutations send a stable UUIDv4-shaped
  `Idempotency-Key`, derived from sha256 of the conversation, the latest inbound
  message, the tool and the canonical UTC-normalized args, so a retry replays.
  The turn's inbound id is the one the gateway loaded at turn start, so tool
  wrappers and the runtime keep their signatures.
  A read timeout or a 502/504 without an envelope on a mutation raises
  `OUTCOME_UNKNOWN` and is not retried. The runtime turns that into a human
  handoff, or re-raises the error if the handoff fails. It never reports the
  outcome as `proposed`.
- `uv.lock`: langgraph 1.2.11 → 1.2.12, langchain 1.4.0 → 1.4.3
  (langchain-core 1.6.2 → 1.6.6). `pyproject.toml` is unchanged.
- Regenerated `docs/api/openapi.json`/`openapi.yaml` (1 new route, additive;
  `conversation_id` becomes optional).

## B0.5 — Domain gaps: outcomes, reversals, reorder points, waitlist, domain events (2026-10-01)

- Migration `0020_domain_gaps`: `ck_appointments_state` widened to
  `completed`/`no_show` (GiST unchanged); new tables `domain_events`,
  `payment_reversals`, `reorder_points`, `waitlist_entries` with composite
  tenant FKs; 5 new permission codes (48 → 53). The downgrade refuses (no data
  rewrite) while `completed`/`no_show` appointments or `payment_reversals`
  rows exist.
- `POST /appointments/{id}/complete` and `/no-show` (`appointments.record_outcome`;
  only `confirmed` appointments that have already started).
- `POST /payments/{id}/reverse`: full reversal as a new row; `payments` is never
  edited and the charge's paid amount stops counting the payment. Human
  principals only (L4): agent, integration and system principals get
  `INVALID_INPUT` before any receipt or read. `PaymentRead` gains
  `reversed`/`reversed_at` (additive).
- `PUT /products/{id}/reorder-points/{location_id}`, `GET /inventory/low-stock`;
  stock-decreasing paths emit `inventory.below_reorder` only when they cross the
  minimum.
- Minimal waitlist: `POST /waitlist`, `GET /waitlist`, `POST /waitlist/{id}/cancel`.
- `record_domain_event` (`app/events/`) stages a `domain_events` row in the
  caller's transaction for `appointment.cancelled|completed|no_show`,
  `payment.recorded|reversed`, `inventory.below_reorder`, `waitlist.created`.
- `reception-staff-demo` (integration) gains 4 of the 5 codes, but not
  `payments.reverse`. The demo seed adds 3 open waitlist entries and reorder points.
- Regenerated `docs/api/openapi.json`/`openapi.yaml` (7 new routes, additive).

## B0 — Demo base: economics/inventory auth, agent proposes only, demo seed (2026-09-30)

- Economics (charges, payments, follow-ups, products, consumptions) and
  inventory (entries, adjustments, transfers, kardex, balance) now require
  `require_authenticated_context` like every other business router; anonymous
  callers get 401 even with `ERP_ANONYMOUS_COMPAT=true`. The route-walk test
  is now behavioural (FastAPI 0.141 mounts included routers lazily, so the old
  static walk checked nothing).
- Tests strip behaviour-changing env vars inherited from a shell that sourced
  `.env.local` (autouse `isolated_environment` fixture in `tests/conftest.py`).
- Sales Agent: `confirm_appointment` is removed from the model's tool list and
  the agent gateway allowlist (6 tools); the backend tool and the human
  confirmation route are unchanged. `SYSTEM_PROMPT` is now Peruvian Spanish and
  states that the agent only proposes and staff confirm.
- New `scripts/seed_demo.py` (deterministic, idempotent, local-DB guard,
  `--issue-staff-credential` → `.env.demo.local`, mode 0600), new
  `reception-staff-demo` profile in `scripts/issue_credential.py`, new
  `scripts/dev_up.sh`.
- No schema or migration change. Regenerated `docs/api/openapi.json`/`openapi.yaml`
  (additive `security: IntegrationBearer` on the 22 economics/inventory operations).

## AGENT-03 — Agent appointment mutations remain human-controlled (2026-09-22)

- Reused the existing human-confirmation guard for cancellation and rescheduling
  commands. Agent principals are refused before command transactions, receipt
  claims, confirmed-proposal shortcuts, and tool-level receipt replay.
- Preserved the existing human operations, IAM permission checks, tenant-scoped
  lookups, proposal expiry, audit/receipt transactions, and booking refusal.
- Added PostgreSQL regressions for direct and tool confirmation of pending and
  confirmed proposals, and for cancellation/rescheduling receipt replay.
- No schema, API, scheduling, or OpenAPI change.

## CORE-02 — Close the ERP_ANONYMOUS_COMPAT gap on protected business routes (2026-09-22)

- The Lead-to-Appointment and Reception/Scheduling business routers
  (commercial, catalog, organization, clinical, scheduling — 27 routes) now
  require `require_authenticated_context` at the router level, the same gate
  already applied to `/internal/` and `/agent-tools/`. A missing or invalid
  credential is rejected with 401 even when `ERP_ANONYMOUS_COMPAT=true`; that
  flag now only affects the unrelated economics and inventory routers.
- The four CORE-01 appointment-proposal routes read the router-level
  resolved context via `resolve_http_context` instead of authenticating a
  second time; their human-only reviewer gate is unchanged.
- Regenerated `docs/api/openapi.json`/`openapi.yaml` (additive `security:
  IntegrationBearer` declarations on the newly protected operations).
- Added `tests/test_core02_business_auth_boundary.py` (57 focused cases:
  missing/invalid credential 401 across all 27 routes, an authenticated
  walk across the full surface, cross-organization denial, and redacted
  audit evidence). Updated `tests/test_security_boundary.py`'s protected-route
  assertions to the new boundary.

## Documentation — Development and agent collaboration (2026-09-17)

- Expanded the backend README with the evidence-first development loop and
  the boundary between deterministic backend business logic and sibling
  adapters.
- Added `docs/AGENT-COLLABORATION.md` as the shared playbook for agents working
  across planning, backend, frontend, voice, simulator, and transport surfaces.
- Added the playbook to the documentation index and development guide without
  changing product code, schema, runtime behavior, or credentials.

## REAL-MODEL-DIAGNOSTICS-01 — Evidence-first Sales Agent failure classification (2026-09-16)

- Preserved the existing generic Sales Agent error envelope while adding a
  sanitized diagnostic log record with trace IDs, execution stage, bounded
  provider/runtime category, upstream status/request ID when supplied by the
  SDK, elapsed time, and observable partial-effect state.
- Classified OpenAI-compatible authentication, rate-limit, invalid-request,
  model-unavailable, server, connection, timeout, invalid-response, gateway,
  and unknown failures without logging exception text, provider bodies,
  credentials, or patient payloads.
- Preserved the one-attempt mutating-turn, typed gateway, fail-closed booking,
  sandbox transport, native OpenAI, and fake-model contracts. No provider
  request, retry, fallback, schema change, or new dependency was added.

## SANDBOX-REAL-MODEL-03 — Bounded real-model execution (2026-09-16)

- Added explicit finite OpenRouter/native-OpenAI model request timeouts, zero
  provider retries, bounded model output tokens, and a cooperative overall
  Sales Agent turn deadline without changing the provider/model boundary.
- Preserved the existing one-attempt mutating turn, typed gateway, fail-closed
  booking proposal, sandbox transport, and fake-model compatibility contracts.

## OPENROUTER-RUNTIME-01 — Provider and local environment bootstrap (2026-09-16)

- Made OpenRouter the explicit Sales Agent development provider with the
  default model `deepseek/deepseek-v4-flash-0731`, using the existing
  `langchain-openai` OpenAI-compatible runtime and
  `https://openrouter.ai/api/v1`.
- Added a loopback-only, idempotent local bootstrap that applies migrations,
  prepares separate agent memory, provisions the existing IAM profiles, and
  preserves existing ignored env values and secrets.
- Added a credential-free preflight; it performs no provider request and
  reports `ready_for_real_model_smoke=false` until `OPENROUTER_API_KEY` is
  supplied.
- No new dependency, model fallback, booking-policy change, sandbox transport
  change, or paid model request was introduced.

## SANDBOX-INBOUND-01 — Controlled end-to-end sandbox loop (2026-09-16)

- Added the development-only `SandboxInboundSender`, which strictly accepts
  `provider=sandbox` events and drives the existing authenticated canonical
  inbound, Sales Agent turn, and outbound persistence contracts in order.
- Added a real-PostgreSQL fake-model regression for one inbound → proposal →
  fail-closed confirmation attempt → sandbox outbound → loopback receipt flow;
  replay, missing/invalid credentials, cross-tenant access, and provider
  isolation remain covered without creating an appointment.
- Reused the existing server-issued `n8n-inbound`, `sales-agent-v0`, and
  `outbound-dispatcher` profiles. No channel framework, consent mechanism,
  live provider, paid model, or production write was added.

## SANDBOX-OUTBOUND-02 — First-class local sandbox delivery (2026-09-16)

- Added the development-only `sandbox` channel provider through additive
  migration `0019`, while preserving `test` as non-dispatchable and
  `whatsapp` as a separate provider.
- Added an authenticated, tenant-scoped local receiver with a durable,
  exact-payload receipt and duplicate replay protection. Sandbox success
  settlement now requires that server-owned receipt; ordinary outbound retry,
  lease, dead-letter, audit and idempotency semantics remain in force.
- Added a bounded one-shot sandbox consumer and local configuration examples;
  it accepts only loopback receiver URLs, requires the existing
  `outbound-dispatcher` credential, and has no live-provider fallback.

## AGENT-TURN-AUTH-01 — Secure Sales Agent entrypoint (2026-09-16)

- Secured `POST /sales-agent/turn` with the existing PostgreSQL-backed bearer
  authentication and permission boundary; only the configured `agent`
  principal can invoke the runtime, with tenant binding and fail-closed
  configuration mismatch handling.
- Updated the WF-01 runner/export to forward the existing agent credential and
  added real-PostgreSQL positive/negative entrypoint coverage. Booking remains
  fail closed for unverified patient acceptance.

## LOCAL-RUNTIME-SMOKE-01 — Reproducible local Lead-to-Appointment smoke (2026-09-16)

- Documented the smallest local smoke path for the existing PostgreSQL CORE,
  FastAPI, and fake-model Sales Agent harness using explicit local database
  settings; no paid model or live channel is required.
- Captured the safe pending-proposal/zero-appointment result, negative-response
  fail-closed result, and the current `provider=test` outbound persistence
  boundary. No dispatcher was added.

## AGENT-CONFIRM-FAIL-CLOSED-03 — Fail closed for unverified agent confirmation (2026-09-16)

- Agent principals can no longer consume a pending appointment proposal into a
  confirmed appointment when the only evidence is a later inbound message;
  the command returns `INVALID_INPUT`, leaves the proposal pending, and keeps
  the audit/error path intact.
- Preserved authenticated human confirmation, the temporal guard, proposal
  token/conversation isolation, expiry, idempotent replay, and exactly-one
  appointment behavior. Automatic agent booking now awaits a verified,
  proposal-bound patient acceptance mechanism.

## AGENT-CONFIRM-GUARD-01 — Enforce patient confirmation (2026-09-16)

- Booking proposal confirmation now requires authoritative persisted evidence of
  a later inbound message in the same conversation before creating an
  appointment; same-turn model claims fail with `INVALID_INPUT`.
- Updated the valid booking fixtures to exercise the required two-message
  proposal/confirmation flow and added a real-PostgreSQL fake-model regression
  covering pending proposal state, zero appointments, isolation, expiry,
  idempotency and audit behavior.

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
