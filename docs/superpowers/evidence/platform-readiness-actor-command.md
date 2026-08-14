# Platform Readiness — Actors / API / Idempotency / Trace (SCOUT B)

Baseline: commit `4086dc1`, 174 tests PASS. Scope: read-only evidence audit.
Sections B1–B7. Every bullet is tagged `[FACT]` (file:line, symbol), `[INFERENCE]`, or `[OPEN QUESTION]`.

---

## B1. Request / session lifecycle

**Route style: sync everywhere.**

- [FACT] All HTTP route handlers are synchronous `def`, never `async def` — FastAPI runs them in the threadpool. Cite: `app/commercial/router.py:11-19` (`create_lead_route`, `get_lead_route`); `app/catalog/router.py:11-19` (`create_service_route`, `list_services_route`); `app/organization/router.py:23-47` (`create_location_route`, `create_practitioner_route`, `create_capability_route`, `list_eligible_practitioners_route`); `app/scheduling/router.py:97-157` (`create_availability_rule_route`, `create_schedule_block_route`, `query_slots_route`, `create_appointment_route`, `cancel_appointment_route`, `reschedule_appointment_route`).
- [FACT] Error handlers are `async def` — `app/errors.py:70` (`app_error_handler`), `:77` (`validation_error_handler`), `:90` (`integrity_error_handler`).
- [FACT] Health endpoint is sync `def` — `app/__init__.py:14-16` (`health`).

**get_db behavior.**

- [FACT] `get_db()` at `app/db.py:18-23` yields one `Session` from `SessionLocal` and `db.close()` in a `finally`; it does NOT open, commit, or roll back a transaction — commit ownership lives in the application service (or the transport error handler).
- [FACT] `SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)` — `app/db.py:11`. `engine = create_engine(settings.database_url, pool_pre_ping=True)` — `app/db.py:10`. Sync psycopg URL in `app/config.py` (`DEFAULT_DATABASE_URL`, `DEFAULT_TEST_DATABASE_URL`).
- [FACT] The only FastAPI dependency used by any router is `get_db` (plus the `get_booking_operation` test seam). Grep of `app/` for `Depends(` returns exclusively `Depends(get_db)` and `Depends(get_booking_operation)` — `app/scheduling/router.py:99,106,113,127,128,146,155`; `app/commercial/router.py:13,19`; `app/catalog/router.py:13,19`; `app/organization/router.py:25,32,39,46`.

**Transaction ownership.**

- [FACT] Explicit `session.begin()` is used ONLY in the scheduling service: `book_appointment` (`app/scheduling/service.py:158`), `cancel_appointment` (`:279`), `reschedule_appointment` (`:328`). All three state that the use case owns its transaction and must receive an idle Session (`app/scheduling/service.py:147-150`, `:269-278`, `:319-327`).
- [FACT] All other mutations rely on SQLAlchemy autobegin + an explicit `session.commit()` inside the service: `create_lead` (`app/commercial/service.py:66-68`), `create_service` (`app/catalog/service.py:18-20`), `create_location` (`app/organization/service.py:23-25`), `create_practitioner` (`:31-33`), `create_capability` (`:52-54`), `create_availability_rule` (`app/scheduling/query.py:67-69`), `create_schedule_block` (`app/scheduling/query.py:93-95`).
- [FACT] Reads commit nothing and rely on session close to discard: `get_lead` (`app/commercial/service.py:72-76`), `list_services` (`app/catalog/service.py:24-25`), `list_eligible_practitioners` (`app/organization/service.py:58-86`), `find_available_slots` (`app/scheduling/query.py:99-173`).
- [INFERENCE] Because `get_db` closes without commit, an `AppError` raised before a service's `commit()` leaves the autobegin transaction implicitly rolled back at session close — no partial write survives. Booking/cancel/reschedule roll back via the `with session.begin()` context manager instead.

**Router-level DB reads before transactional services.**

- [FACT] NONE. Routers are thin pass-throughs (HTTP shape → schema → service). All reads happen inside services, including pre-transaction validation reads inside mutating services, e.g. `create_service` name check `app/catalog/service.py:10`, `create_capability` entity existence checks `app/organization/service.py:40-45`, `create_availability_rule` / `create_schedule_block` active-entity loads `app/scheduling/query.py:54-55,76-77`.
- [FACT] The booking router documents explicitly: "Booking performs no preliminary DB queries here: `book_appointment` owns its transaction and must receive an idle session" — `app/scheduling/router.py:5-7`. The only router-level logic is the deadlock retry wrapper `book_appointment_with_retry` (`app/scheduling/router.py:64-94`), which calls the service, and on `40P01` rolls back and retries the whole operation once; a second deadlock is surfaced as `409 APPOINTMENT_CONFLICT` (`:92-94`).
- [FACT] Cancel/reschedule deliberately do NOT use the retry policy; they take the row `FOR UPDATE` as the first statement so same-row mutations serialize on the lock — `app/scheduling/router.py:135-139` and `_lock_appointment` (`app/scheduling/service.py:218-233`).

**Request-scoped context.**

- [FACT] NONE exists. No `contextvars`, no middleware, no per-request state. Grep for `middleware`, `contextvars`, `Header`, `Request`, `Cookie` across `app/` returns nothing outside `app/errors.py` (exception handlers) and `app/__init__.py`.
- [FACT] `get_booking_operation` (`app/scheduling/router.py:60-61`) is a dependency that returns the `book_appointment` callable — used purely as a test seam (`tests/test_api.py:22,725` override it); it stores no per-request state.
- [OPEN QUESTION] The FastAPI `Request` object is never injected, so there is no current seam to read headers (e.g., correlation or idempotency keys) at the boundary.

---

## B2. Actor / identity today

Exhaustive grep of `app/` for `actor|user|principal|requester|owner|created_by|updated_by|correlation|request_id|session identity|authenticat|authoriz` — all matches:

- [FACT] `app/audit/models.py:15` `actor_id` (String(100), NOT NULL); `:16` `actor_type` (String(50), NOT NULL); `:23` `correlation_id` (String(100), nullable).
- [FACT] `app/audit/service.py:15-16` `SYSTEM_ACTOR_ID = "system"`, `SYSTEM_ACTOR_TYPE = "system"`; `:27-29` `actor_id`/`actor_type`/`correlation_id` kwargs; `:33-34` defaulting `actor_id or SYSTEM_ACTOR_ID`, `actor_type or SYSTEM_ACTOR_TYPE`.
- [FACT] `app/scheduling/service.py:137-139, 210-212, 265-267, 294-296, 307-309, 383-385` — `actor_id`, `actor_type`, `correlation_id` kwargs on `book_appointment`/`cancel_appointment`/`reschedule_appointment` forwarded to `record_event`.
- [FACT] NO `user`, `principal`, `requester`, `owner`, `created_by`, `updated_by`, `request_id`, `authentication`, `authorization` symbols exist anywhere in `app/`. The only textual hits are the word "authority"/"authoritative" in comments at `app/scheduling/service.py:13,319,333` (false positives).
- [FACT] No auth dependency, no token/JWT/header handling, no identity model or table. `app/config.py` defines only `app_env`, `database_url`, `test_database_url`.
- [FACT] There is no `created_by`/`updated_by` column on any entity model (`app/catalog/models.py`, `app/commercial/models.py`, `app/organization/models.py`, `app/scheduling/models.py`) — only `created_at`.

---

## B3. Audit event contract

**Exact schema** (`app/audit/models.py:10-23`, mirrored in migration `alembic/versions/0001_lead_to_appointment.py:129-142`):

| column | type | nullable | notes |
|---|---|---|---|
| `id` | Integer Identity | NO | PK |
| `actor_id` | String(100) | NO | default `"system"` |
| `actor_type` | String(50) | NO | default `"system"` |
| `action` | String(50) | NO | |
| `entity_id` | String(100) | NO | |
| `entity_type` | String(50) | NO | |
| `occurred_at` | DateTime(timezone=True) | NO | `server_default=func.now()` |
| `before_state` | JSONB | YES | |
| `after_state` | JSONB | YES | |
| `correlation_id` | String(100) | YES | |

- [FACT] Indexes: only `ix_audit_events_entity (entity_type, entity_id)` — `app/audit/models.py:12`, migration `:142`. No other index, no unique constraint, no FK.
- [FACT] `actor_type` has NO check constraint or enum — it is free-form text (migration `:133`).

**before/after JSON payload structure.**

- [FACT] Canonical appointment-state payload built by `_appointment_state` (`app/scheduling/service.py:251-258`): `{"id": int, "start_utc": <UTC ISO-8601>, "end_utc": <UTC ISO-8601>, "state": str}`.
- [FACT] Booking: `action="appointment.created"`, `before_state=None`, `after_state` = `{"id", "start_utc", "end_utc", "state":"confirmed"}` — `app/scheduling/service.py:199-213`.
- [FACT] Cancel: `action="appointment.cancelled"`, `before_state` = confirmed payload, `after_state` = `state:"cancelled"` — `app/scheduling/service.py:283-297`.
- [FACT] Reschedule: `action="appointment.rescheduled"`, `before_state` = old interval, `after_state` = new interval — `app/scheduling/service.py:371-386`.
- [FACT] Action constants and entity type: `APPOINTMENT_ENTITY_TYPE="appointment"`, `APPOINTMENT_CREATED_ACTION`, `APPOINTMENT_CANCELLED_ACTION`, `APPOINTMENT_RESCHEDULED_ACTION` — `app/scheduling/service.py:42-45`.
- [FACT] `record_event` only stages the row; it never commits and never opens its own transaction (docstring `app/audit/service.py:1-7`), so the audit row commits atomically with the state transition (`app/scheduling/service.py:158-215` for booking; tests assert exactly one audit row per successful booking and zero on failure — `tests/test_booking.py:384-423`).

**Who / which request / human vs agent?**

- [FACT] Via HTTP, `actor_id` and `actor_type` are ALWAYS `"system"`/`"system"`: the appointment schemas use `extra="forbid"` (`AppointmentCreate` `app/scheduling/schemas.py:55-62`, `AppointmentCancel` `:65-70`, `AppointmentReschedule` `:73-76`) and the routers pass only schema fields / path args to the services — `app/scheduling/router.py:130-132, 148, 157`. `record_event` defaults to `SYSTEM_ACTOR_ID`/`SYSTEM_ACTOR_TYPE` (`app/audit/service.py:33-34`).
- [FACT] At the application-service boundary, caller-supplied actors ARE supported and tested: `book(session, ids, actor_id="recepcion-01", actor_type="staff")` persists on the event — `tests/test_booking.py:405-413`; same for cancel (`tests/test_cancellation.py:151-169`) and reschedule (`tests/test_rescheduling.py:434-445`).
- [FACT] `correlation_id` is NEVER populated via HTTP — no schema field, routers never pass it; via HTTP it is always NULL. Design doc says "Correlation identifier when supplied by the request boundary" (`docs/superpowers/specs/2026-08-12-lead-to-appointment-design.md:150`), but the current request boundary never supplies it.
- [FACT] No `request_id` is generated anywhere; no middleware assigns one.
- [ANSWER] Who caused a mutation: only at the service layer, and only if the caller passes it; via HTTP the answer is always `system`. Which HTTP/tool request: NOT answerable — `correlation_id` is always NULL via HTTP. Human vs agent vs system: NOT answerable — `actor_type` is free-form and always `"system"` via HTTP, with no provenance channel.

---

## B4. Mutating command inventory

All are unauthenticated `POST`s; routers thin; see B1 for transaction ownership.

| Endpoint (HTTP method/path) | App service (symbol) | Transaction | Side effects on success | Duplicate on retry? | Naturally idempotent today? | DB mitigation | Retry → different logical error after success? |
|---|---|---|---|---|---|---|---|
| `POST /leads` (`app/commercial/router.py:11-15`) | `create_lead` (`app/commercial/service.py:46-69`) | autobegin + commit (`:66-68`) | inserts Lead row | YES — new row each retry; no unique constraint on phone/email/name | NO | none (only check constraints `ck_leads_acquisition_source`, `ck_leads_at_least_one_contact`, `app/commercial/models.py:25-33`) | NO — each retry succeeds again (201) |
| `POST /services` (`app/catalog/router.py:11-15`) | `create_service` (`app/catalog/service.py:9-21`) | autobegin + commit (`:18-20`) | inserts Service row | prevented — `services.name` unique (`app/catalog/models.py:13`) | NO (retry ≠ success) | `UNIQUE (name)` | YES — 2nd attempt returns `422 INVALID_INPUT` (`app/catalog/service.py:10-12`); a concurrent race hits unique violation `23505`, re-raised as `500` (only `23P01` is mapped, `app/errors.py:90-103`) |
| `POST /locations` (`app/organization/router.py:23-27`) | `create_location` (`app/organization/service.py:20-26`) | autobegin + commit (`:23-25`) | inserts Location row | YES — duplicates allowed | NO | none | NO — retry succeeds again |
| `POST /practitioners` (`app/organization/router.py:30-34`) | `create_practitioner` (`app/organization/service.py:29-34`) | autobegin + commit (`:31-33`) | inserts Practitioner row | YES — duplicates allowed | NO | none | NO — retry succeeds again |
| `POST /capabilities` (`app/organization/router.py:37-41`) | `create_capability` (`app/organization/service.py:37-55`) | autobegin + commit (`:52-54`) | inserts capability row | prevented — `uq_capabilities_practitioner_service_location` (`app/organization/models.py:44-51`) | NO (retry ≠ success) | `UNIQUE (practitioner_id, service_id, location_id)` | YES — no app-level duplicate check (`app/organization/service.py:40-51`), so a sequential retry raises `IntegrityError` (23505) → re-raised as `500` |
| `POST /availability-rules` (`app/scheduling/router.py:97-101`) | `create_availability_rule` (`app/scheduling/query.py:51-70`) | autobegin + commit (`:67-69`) | inserts rule row | YES — identical rules can be duplicated | NO | none (only interval/weekday checks, `app/scheduling/models.py:26-29`) | NO — retry succeeds again |
| `POST /schedule-blocks` (`app/scheduling/router.py:104-108`) | `create_schedule_block` (`app/scheduling/query.py:73-96`) | autobegin + commit (`:93-95`) | inserts block row | YES — identical blocks can be duplicated | NO | none (only `ck_schedule_blocks_interval`, `app/scheduling/models.py:45-47`) | NO — retry succeeds again |
| `POST /appointments` (`app/scheduling/router.py:124-132`) | `book_appointment` (`app/scheduling/service.py:129-215`) | `with session.begin()` (`:158`) | inserts confirmed appointment + one `appointment.created` audit row (same txn) | prevented — slot now occupied by the first confirmed row | NO | partial GiST exclusion `excl_appointments_confirmed_no_overlap` (`app/scheduling/models.py:68-78`), `23P01 → 409` (`app/errors.py:90-102`) | YES — 2nd attempt fails preflight `SLOT_BLOCKED` 409 (`app/scheduling/service.py:181-185`) or GiST `APPOINTMENT_CONFLICT` 409; deadlock `40P01` retried once (`app/scheduling/router.py:64-94`) |
| `POST /appointments/{id}/cancel` (`app/scheduling/router.py:142-148`) | `cancel_appointment` (`app/scheduling/service.py:261-299`) | `with session.begin()` (`:279`) | state→cancelled + one `appointment.cancelled` audit row | prevented — 2nd cancel raises `ENTITY_INACTIVE` (`app/scheduling/service.py:236-248,281`) | NO — deliberately non-idempotent (docstring `:236-248`) | none (state check under `FOR UPDATE`) | YES — 2nd attempt returns `409 ENTITY_INACTIVE` "not confirmed"; `404` only if the row is gone |
| `POST /appointments/{id}/reschedule` (`app/scheduling/router.py:151-157`) | `reschedule_appointment` (`app/scheduling/service.py:302-388`) | `with session.begin()` (`:328`) | updates same row to new interval + one `appointment.rescheduled` audit row | state-wise NO (row already at `new_start`, self-excluded from conflict check) BUT each retry appends an audit row with `before == after` | PARTIAL (final state stable; audit side effect repeats) | GiST exclusion on the *new* interval (`app/scheduling/service.py:348-369`) | YES — if the target interval became blocked meanwhile, retry returns `409 SLOT_BLOCKED`; otherwise it "succeeds" (200) again writing an audit event |

- [FACT] `book_appointment` retry wrapper `book_appointment_with_retry` (`app/scheduling/router.py:64-94`) retries only `40P01` once; it is a transport-level retry of the whole operation on the same Session after `session.rollback()` — it is not client-facing idempotency.
- [FACT] Tests confirm the concurrency behavior: two racing bookings persist exactly one appointment and one audit row, loser fails with `23P01`/`40P01` (`tests/test_booking.py:449-520`); a post-preflight committed row yields `23P01` (`tests/test_booking.py:522-590`).
- [INFERENCE] The five "configuration" endpoints (services, locations, practitioners, capabilities, availability-rules, schedule-blocks) plus leads accept identical repeated requests without error — retries by an external caller silently create duplicates, with no signal to the caller.

---

## B5. Idempotency requirements

- [FACT] Classification of current behavior (same exact request, no idempotency key):
  - SAFE RETRY (no state change on retry): NONE — every mutation either duplicates state, errors differently, or (reschedule) rewrites an audit row.
  - CONDITIONALLY SAFE (DB prevents duplication but retry changes the response / errors): `create_service` (retry → 422; race → 500), `create_capability` (retry → 500), `book_appointment` (retry → 409, but no double booking), `cancel_appointment` (retry → 409, no double cancellation), `reschedule_appointment` (final state stable, but repeated audit rows; 409 if interval becomes blocked).
  - NOT IDEMPOTENT (retry duplicates the side effect): `create_lead`, `create_location`, `create_practitioner`, `create_availability_rule`, `create_schedule_block`.
- [INFERENCE] Highest need for durable command identity once autonomous agents / external integrations call in:
  1. `POST /appointments` (book) — a timeout after a successful booking is indistinguishable from a rejection today: retry yields `409`, never the created appointment (`app/scheduling/service.py:181-185`).
  2. `POST /appointments/{id}/reschedule` — retries are ambiguous (may 200 again and duplicate audit events) and a client cannot tell "already applied" from "needs applying".
  3. `POST /leads` and the configuration creates (`location`, `practitioner`, `availability-rules`, `schedule-blocks`) — silent duplicates.
  4. `POST /appointments/{id}/cancel` — retry semantics are a deliberate stable conflict (`app/scheduling/service.py:236-248`); an agent retry must know to treat the 409 as "already done".
  5. `POST /capabilities` / `POST /services` — retries surface non-conflict error codes (500 / 422) that mislead retry logic.
- [OPEN QUESTION] No `Idempotency-Key` header, no `request_id`, no command table, and no unique key on any mutation request payload exist today — durable command identity would be net-new.

---

## B6. Agent-to-tool readiness

- [FACT] FastAPI/OpenAPI boundary exists (`app/__init__.py:10-26`); all routes are sync and produce typed Pydantic responses; error envelope is stable machine-readable `{"error": {"code", "message", "details"}}` (`app/errors.py:54-55`, asserted by `tests/test_errors.py`).
- [FACT] NO authentication or authorization exists anywhere in `app/` (see B2). Every mutation and read endpoint is publicly exposed.
- [FACT] No LLM/agent libraries are imported: grep of `app/` for `langchain|langgraph|openai|anthropic|llm|agent` returns no files.
- [INFERENCE] Safely mappable to agent tools today (read-only, idempotent): `GET /health`, `GET /services`, `GET /leads/{id}`, `GET /practitioners/eligible`, `POST /slots/query`.
- [INFERENCE] Mappable only after authorization + idempotency/actor plumbing: all eight mutating `POST` endpoints (B4). Booking/cancel/reschedule additionally need command identity before an agent can act on retries.
- [INFERENCE] Generic/unbounded mutation capability: `POST /services`, `POST /locations`, `POST /practitioners`, `POST /capabilities`, `POST /availability-rules`, `POST /schedule-blocks` are administrative configuration writes with no ownership scoping, no tenant/principal, and no rate limiting — exposing them as tools without authorization lets any caller reconfigure the schedule/catalog.
- [FACT] `actor_id`/`actor_type`/`correlation_id` are accepted only at the service layer (`app/scheduling/service.py:137-139`), so a future tool layer can supply them without changing the HTTP contract — but the HTTP layer currently cannot.

---

## B7. External-integration risks (Calendar / WhatsApp / email sync later)

Contracts that ALREADY exist and are favorable:

- [FACT] Stable resource identity: every resource uses an integer `Identity` PK (`app/catalog/models.py:12`, `app/commercial/models.py:14`, `app/organization/models.py:14,24,33`, `app/scheduling/models.py:15,35,53`) — appointments reference leads/services/practitioners/locations by stable FK ids.
- [FACT] Source-of-truth boundary is clean: `appointments` is the authoritative row; `book_appointment`/`cancel_appointment`/`reschedule_appointment` are the only mutators; reschedule updates the SAME row (no create+delete churn) — `app/scheduling/service.py:328-388`. Cancel preserves the interval and only flips `state` (`:279-299`). All appointment state stored UTC with half-open `[start, end)` (GiST `tstzrange(start_utc, end_utc, '[)')`, `app/scheduling/models.py:73-78`).
- [FACT] Audit is append-only and atomic with each transition (B3) — a durable local trace exists for every appointment mutation.
- [FACT] Error contract gives external callers stable codes (`409 APPOINTMENT_CONFLICT` etc., `app/errors.py:8-24`).

Gaps vs. "OdontoFlow transaction → durable external sync":

- [FACT] Command identity: NONE — no idempotency key, no command/outbox table, no unique key on request payloads (B4/B5). A Calendar/WhatsApp outbound sync triggered from a booking retry would double-fire.
- [FACT] Correlation across systems: `correlation_id` column and service param exist (`app/audit/models.py:23`, `app/scheduling/service.py:139`) but the HTTP boundary never populates it and no `request_id` is generated (B3). Outbound integration events cannot be correlated to the request that caused them.
- [FACT] No event/outbox mechanism exists: the only durable record of a transition is `audit_events`; there is no "sync state", "external reference id", or delivery-failure table, and no queueing infra in `requirements`/config (no Kafka/Temporal/Redis anywhere in the repo).
- [INFERENCE] To preserve for later sync: (1) integer PK identity (exists), (2) single-row reschedule semantics (exists — a Calendar sync keyed on appointment id survives reschedules without re-sync churn), (3) atomic audit with stable `action` vocabulary (exists), (4) per-request correlation + actor population at the HTTP boundary (missing), (5) durable command identity for at least book/reschedule/cancel (missing).
- [OPEN QUESTION] Whether external sync needs an outbox/message infrastructure is undetermined — no current evidence (no multi-system writes, no delivery requirements) forces it; the minimal prerequisite is command identity + correlation, neither of which requires an external message broker.

---

## Mutation / idempotency matrix

See the table in B4 (columns: endpoint, service, transaction ownership, side effects, retry duplication, natural idempotency, DB mitigation, retry error divergence).

Summary of current idempotency posture:

| Mutation | Retry duplicates state? | DB prevents duplication? | Retry after success → same success? | Verdict today |
|---|---|---|---|---|
| create lead | yes | no | no (succeeds again, 201) | NOT IDEMPOTENT |
| create service | no | unique name | no (422; race → 500) | CONDITIONALLY SAFE |
| create location | yes | no | no | NOT IDEMPOTENT |
| create practitioner | yes | no | no | NOT IDEMPOTENT |
| create capability | no | unique triple | no (500) | CONDITIONALLY SAFE |
| create availability rule | yes | no | no | NOT IDEMPOTENT |
| create schedule block | yes | no | no | NOT IDEMPOTENT |
| book appointment | no | GiST exclusion | no (409) | CONDITIONALLY SAFE (needs command identity) |
| cancel appointment | no | state guard | no (409) | CONDITIONALLY SAFE (deliberately non-idempotent) |
| reschedule appointment | no (state) / yes (audit) | GiST on new interval | no (200 again + duplicate audit) | CONDITIONALLY SAFE / partial |

---

## Top-5 gap list

1. **No command identity.** No idempotency key, no command table, no unique key on request payloads — every mutation's retry semantics are either duplicates, non-conflict errors (422/500), or a 409 that hides prior success. Highest risk: booking and reschedule by autonomous agents/external callers.
2. **No request-scoped actor or correlation at the HTTP boundary.** HTTP audit rows are always `actor_id/actor_type = "system"` and `correlation_id = NULL`; schemas `extra="forbid"` and routers never pass them, and no middleware/`Request` injection exists. "Who / which request / human vs agent" is unanswerable from HTTP traffic.
3. **No authentication or authorization.** All 8 mutating POSTs and all reads are unauthenticated; 5 configuration endpoints expose unbounded administrative mutation (catalog, locations, practitioners, capabilities, availability, blocks) with no principal, tenant, or ownership scoping.
4. **Silent-duplicate configuration/lead endpoints.** `create_lead`, `create_location`, `create_practitioner`, `create_availability_rule`, `create_schedule_block` accept identical retries without error — duplicates are persisted with no caller-visible signal.
5. **Non-conflict error codes on dedup paths.** `create_service` returns 422 and `create_capability` returns 500 (unmapped unique-violation `23505`) on retries instead of a stable 409, which breaks client/agent retry logic; only SQLSTATE `23P01` is mapped in the transport (`app/errors.py:90-103`).
