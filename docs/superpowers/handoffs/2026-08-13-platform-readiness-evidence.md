# Platform Readiness Gate — Consolidated Evidence Report

**Baseline:** `4086dc1` (test: close lead-to-appointment vertical with e2e) · Full suite: **174 PASS** · Working tree clean · MediStock untouched.
**Method:** two read-only DeepSeek V4 Flash evidence audits (Scout A — data ownership/tenancy; Scout B — actors/API/idempotency/trace). Facts cite file:line and symbol. No design decisions are made here; no implementation.
**Source evidence artifacts:**
- `docs/superpowers/evidence/platform-readiness-data-ownership.md` (Scout A)
- `docs/superpowers/evidence/platform-readiness-actor-command.md` (Scout B)

---

## 1. Baseline

- Commit `4086dc1`; suite 174 PASS; single migration `0001_lead_to_appointment.py`; no CLAUDE.md/AGENTS.md/GEMINI.md invariants file exists in OdontoFlow.
- No `Organization`/`Tenant`/`Clinic` concept anywhere (`grep tenant|organization|org_id|clinic` → only the `organization` module name).

## 2. Current ownership / FK graph

```
(global, no parent)        (global, no parent)        (global, no parent)
 services                    practitioners              leads ──(nullable service_need_id)──► services
   ▲ service_id                ▲ practitioner_id
   │                           │                        audit_events (no FKs; entity_id String(100))
 practitioner_capabilities ◄───┘                        locations (no parent) ◄── location_id ──┐
   (practitioner,service,location) UNIQUE               appointments ──► lead_id/service_id/practitioner_id/location_id
   availability_rules ──► practitioner_id, location_id  schedule_blocks ──► practitioner_id, location_id
   GiST exclusion: (practitioner_id, tstzrange) WHERE state='confirmed'   [practitioner-wide, NOT location-scoped]
```
[FACT] All 8 PKs are integer `Identity()`; every FK `ondelete="RESTRICT"`; `Location` is the only organizational grouping (has children, no parent); `Service`, `Practitioner`, `Lead`, `AuditEvent` have no location/org FK. Full evidence: Scout A §A1.

## 3. Current ID strategy

- [FACT] All PKs integer `Identity()`, exposed as `int` in all routes/schemas/responses; no production ordering depends on id (services order by name, practitioners by display_name, slots by time). Audit `entity_id` is `String(100)` written as `str(appointment.id)` — the only UUID-tolerant place today.
- [FACT] Sequential-id ordering reliance exists only in tests (`tests/test_lead_to_appointment_e2e.py:383,406` order by id).
- [INFERENCE] Keeping int PKs is the zero-cost baseline. Converting PKs to UUID would touch 8 models, all FK columns, all schemas/routes/services, tests, and the reschedule self-exclusion predicate; the least-invasive later option is a `public_id` column while keeping the int PK spine. No auth exists, so int IDs are fully enumerable today. Full evidence: Scout A §A4.

## 4. Multi-tenancy blast radius (Organization → many Locations)

| Entity | Today | Likely needs | Evidence |
|---|---|---|---|
| Service | global, name UNIQUE global | DIRECT org ownership (per-org catalogs) OR declared shared master | `app/catalog/models.py:13`, `app/catalog/service.py:10-12` |
| Practitioner | no FK; membership only via capability rows | junction table for multi-org; else single-org assumed | `app/organization/models.py:21-27` |
| PractitionerCapability | triple UNIQUE, globally separated by FK uniqueness | constraint OK; query-scoping may need org | `app/organization/models.py:44-50` |
| Lead | no anchor; derivable only via existing Appointment | DIRECT org/location at creation | `app/commercial/models.py:14-21` |
| Appointment | carries all 4 FKs incl. location | DERIVED via location (if 1:1 org) — widest blast radius if changed | `app/scheduling/models.py:53-57` |
| AvailabilityRule/ScheduleBlock | carry practitioner+location | DERIVED via location; cheapest to scope | `app/scheduling/models.py:15-17,35-37` |
| AuditEvent | no FK, polymorphic entity_id, org not captured at write | DIRECT org column written at event time or unrecoverable | `app/audit/models.py:12-18`, `app/scheduling/service.py:202,290,379` |

## 5. Constraints that MAY need tenant-awareness

- `services.name` UNIQUE — **will collide** across orgs with per-org catalogs (`app/catalog/models.py:13`).
- `uq_capabilities_practitioner_service_location` — already naturally separated by globally-unique FKs; org column would be for scoping, not correctness.
- GiST `excl_appointments_confirmed_no_overlap` — **practitioner-global by design** (comments at `app/scheduling/models.py:71-78`, `query.py:141-143`, `service.py:111-113`). Prevents cross-org double-booking today (feature for single-org practitioners; hard blocker for multi-org practitioners).
- All CHECK constraints are value-domain; none tenant-sensitive.

## 6. AuditEvent capabilities / gaps

- Columns: id, actor_id (NOT NULL, defaults "system"), actor_type (NOT NULL, free-form), action, entity_id, entity_type, occurred_at, before_state/after_state JSONB, correlation_id (nullable). Index only `(entity_type, entity_id)`. (`app/audit/models.py:10-23`)
- Via HTTP: `actor_id/actor_type` always `"system"`, `correlation_id` always NULL (schemas `extra="forbid"`, routers never pass them). At the service layer actors ARE supported and tested (`tests/test_booking.py:405-413`).
- [ANSWER] WHO: only if a caller supplies it at the service boundary; via HTTP always "system". WHICH request: not answerable (no correlation/request_id). Human vs agent vs system: not answerable (actor_type free-form, no provenance channel).

## 7. Request / session / transaction lifecycle

- All route handlers are sync `def` (threadpool); error handlers async. `get_db` yields a Session, closes it, never commits (`app/db.py:18-23`).
- Explicit `session.begin()` only in booking/cancel/reschedule (`app/scheduling/service.py:158,279,328`); all other mutations autobegin + explicit `commit()`. Routers perform zero DB reads (thin pass-through); booking router has the single `40P01` one-retry wrapper (`app/scheduling/router.py:64-94`).
- No middleware, no contextvars, no `Request` injection, no request-scoped context exists.

## 8. Current actor/auth/correlation reality

- **NONE** for user/principal/owner/created_by/updated_by/request_id/auth/authorization anywhere in `app/`. Only `actor_id/actor_type/correlation_id` on AuditEvent + service kwargs. No auth dependency on any route; no identity model. (Scout B §B2, exhaustive grep)

## 9. Mutation / idempotency matrix

| Mutation | Retry duplicates? | DB prevents? | Retry-after-success | Verdict |
|---|---|---|---|---|
| create lead | yes | no | succeeds again (201) | NOT IDEMPOTENT |
| create service | no | unique name | 422 (race → 500) | CONDITIONALLY SAFE |
| create location | yes | no | succeeds again | NOT IDEMPOTENT |
| create practitioner | yes | no | succeeds again | NOT IDEMPOTENT |
| create capability | no | unique triple | 500 (23505 unmapped) | CONDITIONALLY SAFE |
| create availability rule | yes | no | succeeds again | NOT IDEMPOTENT |
| create schedule block | yes | no | succeeds again | NOT IDEMPOTENT |
| book appointment | no | GiST 23P01 | 409 (SLOT_BLOCKED/APPOINTMENT_CONFLICT) | CONDITIONALLY SAFE (needs command identity) |
| cancel appointment | no | state guard FOR UPDATE | 409 ENTITY_INACTIVE | CONDITIONALLY SAFE (deliberately non-idempotent) |
| reschedule appointment | state no / audit yes | GiST on new interval | 200 again + duplicate audit row | CONDITIONALLY SAFE / partial |

Highest need for durable command identity once agents/integrations arrive: **book**, **reschedule**, **cancel**, then lead/config creates.

## 10. Agent-tool readiness

- Read-only endpoints safely mappable today: `GET /health`, `GET /services`, `GET /leads/{id}`, `GET /practitioners/eligible`, `POST /slots/query`.
- All 8 mutating POSTs need authorization + actor/correlation + command identity before exposure; the 6 admin-config endpoints are unbounded administrative writes with no ownership scoping.
- [FACT] No LLM/agent library imported anywhere in `app/` (grep langchain|langgraph|openai|anthropic|llm|agent → none).

## 11. External-integration readiness gaps

- Favorable today: stable int identity; single-row reschedule semantics (Calendar sync survives reschedules); atomic append-only audit with stable action vocabulary; stable error codes.
- Gaps: no command identity (a sync triggered from a booking retry would double-fire); `correlation_id` never populated at HTTP boundary; no request_id; no event/outbox mechanism. No evidence forcing Kafka/Temporal/Redis — minimal prerequisite is command identity + correlation at the boundary.

## 12. Decisions easy to defer

- Async migration (no evidence of need; all sync works).
- RLS/row-level security (no tenant table exists to scope).
- Converting integer PKs to UUID (least-invasive path keeps int spine + optional public_id later).
- Outbox/message infrastructure (no multi-system writes yet).
- RBAC model (no users exist).

## 13. Decisions that become expensive after Clinical Bridge

1. **Lead/Patient tenancy anchor** — a never-booked lead (and any future Patient/Charge/Payment attached to it) has no derivable org; retrofitting requires backfill that can only guess. `app/commercial/models.py:14-21`.
2. **Audit tenant attribution** — org must be captured at write time; retroactive attribution of `audit_events` is impossible (no FK, polymorphic entity_id). `app/audit/models.py:12-18`.
3. **Global `services.name` UNIQUE** — first cross-org name collision breaks the constraint; org-scoping the catalog changes the UNIQUE + app checks + every catalog query.
4. **GiST practitioner-global exclusion** — multi-org practitioners require changing the exclusion key, the index, and the widest code path (overlap preflight).
5. **Actor/correlation at the HTTP boundary** — wiring it later means changing every router/schema after integrations and agents may already depend on the current contract.

## 14. Open PRODUCT questions

1. Tenant granularity: Organization per practice, per location, or chain with shared practitioners/services?
2. Is Service a shared procedure ontology (global master) or org-owned product data?
3. Must a Practitioner be multi-org? (GiST + absence of junction table assume single-org.)
4. Should Lead (and future Patient) carry org/location at creation, and which location when lead precedes any booking?
5. Is "practitioner cannot be double-booked across orgs" a hard domain invariant (current) or within-org only?

## 15. Open TECHNICAL questions

1. Direct `organization_id` vs derived-via-location for Service/Lead/AuditEvent?
2. Public identifier strategy: keep int, or add public_id (UUID/ULID) while keeping the int FK spine?
3. Command identity mechanism: Idempotency-Key header vs command table vs unique request hash — and for which commands first?
4. How to populate actor/correlation at the HTTP boundary without auth (trusted headers? gateway?) before auth exists?
5. Audit `actor_type` vocabulary (human/staff/agent/system) — free-form today, needs a closed set before agents write events.

---

## Issue table

| ISSUE | CURRENT EVIDENCE | CHANGE REQUIRED BEFORE CLINICAL BRIDGE? | NEEDS PRODUCT DECISION? | NEEDS CONSULTANT REVIEW? | CONFIDENCE |
|---|---|---|---|---|---|
| Lead (future Patient/Charge/Payment) has no tenancy anchor | `app/commercial/models.py:14-21`; only derivable via existing Appointment | YES (before Patient exists) | YES (tenant granularity) | no | HIGH |
| Audit history not tenant-attributable | `app/audit/models.py:12-18`; org never captured at write | YES (before agents write events) | no | no | HIGH |
| `services.name` global UNIQUE blocks per-org catalogs | `app/catalog/models.py:13`, `app/catalog/service.py:10-12` | only if per-org catalogs chosen | YES (master vs per-org) | no | HIGH |
| GiST exclusion is practitioner-global | `app/scheduling/models.py:73-78`; preflight `query.py:141-143` | only if multi-org practitioners | YES | YES (overlap semantics) | MEDIUM |
| No actor/correlation at HTTP boundary | Scout B §B2/B3; schemas `extra="forbid"`, routers never pass actor | YES before agent tools | no | no | HIGH |
| No command identity (idempotency) | Scout B §B4/B5 matrix; book/reschedule/cancel retry ambiguity | YES before agent tools/integrations | partial (key scope) | YES (protocol choice) | HIGH |
| No auth on any endpoint; int IDs enumerable | Scout B §B2/B6; routers have no auth dependency | YES before any tool exposure | YES (who can do what) | YES | HIGH |
| Silent-duplicate config endpoints | `create_lead/location/practitioner/availability-rule/schedule-block` | deferrable | no | no | MEDIUM |
| 23505 unmapped → 500 on service/capability races | `app/errors.py:90-103` maps only 23P01 | deferrable (fix is small) | no | no | MEDIUM |
| Integer PKs vs public UUID | Scout A §A4 blast radius | deferrable (public_id additive later) | YES | no | MEDIUM |

---

## Verification

- Production code: **unchanged** (`git diff` on `app/`, `alembic/`, `tests/` empty — only the three documentation artifacts are new).
- Migrations: **unchanged** (single `0001`).
- MediStock: **untouched**.
- Full suite: **174 PASS** re-run at baseline.

**Evidence artifacts (committed with this report):**
- `docs/superpowers/evidence/platform-readiness-data-ownership.md`
- `docs/superpowers/evidence/platform-readiness-actor-command.md`
- `docs/superpowers/handoffs/2026-08-13-platform-readiness-evidence.md` (this report)
