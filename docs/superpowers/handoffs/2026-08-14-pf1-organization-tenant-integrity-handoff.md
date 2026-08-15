# PF1 — Organization & Tenant Integrity · Handoff

**Date:** 2026-08-14 · **Baseline SHA:** `0601b09` (174 PASS) · **Result:** `217 passed` · **Not committed**
**Authority:** `docs/superpowers/specs/2026-08-14-platform-foundation-design.md` (PF0), §5–§9, §17, §19, §21 (PF1 block)
**Nature:** IMPLEMENTATION. New migration `0002`, new tenant model, no behaviour change for Vertical 1.

---

## 1. Objective

Make `Organization` the tenant root and make cross-tenant relational states **impossible in PostgreSQL**,
with zero behaviour change for the Vertical 1 lead-to-appointment flow (PF0 §21 PF1 goal).

Delivered:

- `organizations` (tenant root) and `practitioner_memberships` (a global practitioner reaching a tenant).
- Direct `organization_id NOT NULL` on the eight tenant-owned tables.
- The complete §7.2 composite-FK invariant set, so an appointment mixing tenants cannot be written even with
  every application check bypassed.
- `services.name` uniqueness moved from global to per organization.
- Organization-scoped catalog / eligibility / scheduling / appointment **reads**.
- One `resolve_organization_id()` seam supplying the bootstrap organization while no identity exists.
- Migration `0002` with a deterministic, non-destructive backfill of every existing row.
- `excl_appointments_confirmed_no_overlap` **unchanged** — the overlap invariant stays practitioner-global.

---

## 2. Files changed

### New

| Path | What |
|---|---|
| `alembic/versions/0002_organization_tenant_integrity.py` | migration `0001 → 0002`: create + seed, add nullable columns, backfill, tighten; full downgrade |
| `app/tenancy.py` | `BOOTSTRAP_ORGANIZATION_ID`, `BOOTSTRAP_ORGANIZATION_NAME`, `resolve_organization_id()`, `scoped()` — the single tenant seam and the single read-scoping helper |
| `tests/test_tenant_integrity.py` | 41 PF1 proofs against real PostgreSQL |

### Models

| Path | Change |
|---|---|
| `app/organization/models.py` | **new** `Organization`, **new** `PractitionerMembership`; `Location` gains `organization_id` + `UNIQUE (organization_id, id)`; `PractitionerCapability` gains `organization_id` + 3 composite FKs + `ix_capabilities_organization_service_location`; `Practitioner` unchanged (stays global, no tenant column); `uq_capabilities_practitioner_service_location` unchanged |
| `app/catalog/models.py` | `Service.organization_id`; global `name` UNIQUE removed; `UNIQUE (organization_id, name)`, `UNIQUE (organization_id, id)` |
| `app/commercial/models.py` | `Lead.organization_id`; `UNIQUE (organization_id, id)`; composite FK `(organization_id, service_need_id)` → `services(organization_id, id)` (MATCH SIMPLE) |
| `app/scheduling/models.py` | `organization_id` on `AvailabilityRule`, `ScheduleBlock`, `Appointment`; membership + location composite FKs on all three; lead + service composite FKs and `UNIQUE (organization_id, id)` on `Appointment`; **GiST exclusion untouched** |
| `app/audit/models.py` | `organization_id NOT NULL` + plain FK (§14 D6) + `ix_audit_events_organization (organization_id, occurred_at)` |

### Services / query

| Path | Change |
|---|---|
| `app/catalog/service.py` | `create_service(..., organization_id=None)` with org-scoped duplicate check; `list_services(..., organization_id=None)` filtered |
| `app/organization/service.py` | **new** `create_organization` (audited against its own id, D7), **new** `add_practitioner_membership`, **new** `load_membership`; `create_location`, `create_practitioner` (global identity + membership), `create_capability` (all references resolved inside the org), `list_eligible_practitioners` (membership join, both activity flags) |
| `app/commercial/service.py` | `create_lead` org-owned; `service_need` validated inside the org; `get_lead` tenant-scoped |
| `app/scheduling/query.py` | `create_availability_rule` / `create_schedule_block` org-scoped with membership check; `find_available_slots` org-scoped for service/location/rules/blocks — **conflicting-appointment read left practitioner-global** |
| `app/scheduling/service.py` | `book_appointment` / `cancel_appointment` / `reschedule_appointment` take `organization_id`; tenant-scoped entity loads; `_lock_appointment` locks inside the tenant filter; appointment carries its org; audit rows carry the acting org; **overlap preflight scope unchanged** |
| `app/audit/service.py` | `record_event(..., organization_id=<required kwarg>)` — no default, tenant attribution written at event time (F-17) |

### Not touched (verified by `git status` / `git diff`)

`app/errors.py` (error contract), `app/db.py`, `app/scheduling/availability.py` (pure engine), `app/__init__.py`
(router wiring), **every router**, **every Pydantic schema**, `alembic/versions/0001_*.py`, `alembic/env.py`,
the PF0 spec, `../../AI-EdgeRunners/medistock`.

### Tests adapted (fixtures only — see §8)

`tests/conftest.py`, `tests/test_schema_constraints.py`, `tests/test_booking_invariant.py`,
`tests/test_booking.py`, `tests/test_cancellation.py`, `tests/test_rescheduling.py`, `tests/test_api.py`,
`tests/test_errors.py`, `tests/test_migrations.py`.
Untouched test files: `test_catalog_organization.py`, `test_lead.py`, `test_lead_to_appointment_e2e.py`,
`test_availability.py`, `test_health.py`.

---

## 3. Deviation from the task brief (deliberate, spec-driven)

The brief asked for `organization_id` in create/query/booking **request schemas**. PF0 forbids exactly that:

- §21 PF1 "Explicitly NOT included": *request/response schema changes*.
- §19.2: *"**No request schema changes in PF1.** All Pydantic schemas use `extra="forbid"`; adding an optional
  `organization_id` body field would alter the public contract and the OpenAPI surface for no benefit."*

The spec is the named authority ("implement EXACTLY from it"), and the constraint is load-bearing: the
Vertical 1 E2E asserts exact response key sets and 422-on-extra-body-field, so a tenant field in the schemas
would have broken the frozen HTTP contract. Implemented instead, exactly as §19.2 prescribes: services take an
explicit `organization_id`, and the one `resolve_organization_id()` seam supplies the bootstrap organization
when no caller does. **Routers and schemas therefore have a zero-line diff**, and PF3 replaces the seam's body
with `ctx.organization_id` and drops the parameter defaults.

Second, smaller deviation, same reason: the brief described `appointments.practitioner_id` as a plain FK, while
§6/§7.2/PM2/F-3b require the composite FK `(organization_id, practitioner_id)` → `practitioner_memberships`.
The spec version is implemented — it is what makes "a practitioner who does not work here cannot appear in this
organization's schedule" a database fact. The plain FK to `practitioners` from `0001` is retained alongside it.

---

## 4. Entity ownership changes

| Entity | PF1 ownership | Notes |
|---|---|---|
| `Organization` | tenant root | `id` int Identity, `name`, `created_at` |
| `Location` | direct `organization_id` NOT NULL | referenced key `UNIQUE (organization_id, id)` |
| `Service` | direct `organization_id` NOT NULL | `UNIQUE (organization_id, name)` + `UNIQUE (organization_id, id)` |
| `Lead` | direct `organization_id` NOT NULL | `UNIQUE (organization_id, id)`; nullable service need via MATCH SIMPLE |
| `Practitioner` | **GLOBAL — unchanged** | no tenant column; `is_active` = platform kill switch |
| `PractitionerMembership` | direct `organization_id` NOT NULL | `UNIQUE (organization_id, practitioner_id)`, `UNIQUE (organization_id, id)`, `is_active` = works here |
| `PractitionerCapability` | direct `organization_id` NOT NULL | 3 composite FKs; triple UNIQUE unchanged (PM7) |
| `AvailabilityRule` / `ScheduleBlock` | direct `organization_id` NOT NULL | membership + location composite FKs |
| `Appointment` | direct `organization_id` NOT NULL | 4 composite FKs + `UNIQUE (organization_id, id)` |
| `AuditEvent` | direct `organization_id` NOT NULL | plain FK only (D6); `entity_id` stays `String(100)` |

`Principal`, `Membership`, `Permission`, `Role`, `RoleAssignment`, `CommandReceipt`, `ExecutionContext`,
authorization, RLS: **not implemented** (PF2–PF4), as PF1 excludes them.

---

## 5. Composite FK strategy implemented (PF0 §7.2 — complete)

| Child | Constraint | Impossible state |
|---|---|---|
| `leads` | `fk_leads_organization_service_need` → `services(organization_id, id)` | a lead needing another tenant's service |
| `practitioner_capabilities` | `fk_capabilities_organization_membership` → `practitioner_memberships(organization_id, practitioner_id)` | a capability naming a non-member practitioner |
| `practitioner_capabilities` | `fk_capabilities_organization_service`, `fk_capabilities_organization_location` | a capability mixing tenant resources |
| `availability_rules` | `fk_availability_rules_organization_membership`, `fk_availability_rules_organization_location` | availability published at another tenant's branch / for a non-member |
| `schedule_blocks` | `fk_schedule_blocks_organization_membership`, `fk_schedule_blocks_organization_location` | a block at another tenant's branch |
| `appointments` | `fk_appointments_organization_lead`, `_service`, `_membership`, `_location` | **the canonical case**: any tenant mix across lead/service/practitioner/location |
| `audit_events` | `fk_audit_events_organization` (plain) | an unattributable event |

Plus plain `organization_id` FKs on all eight tables. Every FK is `ON DELETE RESTRICT` — no CASCADE, no SET
NULL, consistent with `0001`. `MATCH FULL` is used nowhere: PostgreSQL's default MATCH SIMPLE is required so
`leads.service_need_id IS NULL` skips its composite check (§7.3), proven by
`test_lead_without_service_need_satisfies_the_composite_fk`.

FK count on `(appointments, leads, practitioner_capabilities, availability_rules, schedule_blocks)`:
**12 → 29** (12 Vertical 1 + 5 plain org + 12 composite).

Indexes added: `ix_audit_events_organization (organization_id, occurred_at)`,
`ix_capabilities_organization_service_location`, `ix_availability_rules_organization_location`,
`ix_schedule_blocks_organization_location`, `ix_practitioner_memberships_practitioner`. No separate
single-column `organization_id` index was created on `services` / `locations` / `leads` / `appointments`:
their `UNIQUE (organization_id, id)` index already leads with `organization_id` and serves every tenant-scoped
scan, so an extra index would only cost write throughput.

---

## 6. Migration / backfill detail (`0002`, one transaction, additive)

Staged exactly per §19.1:

1. **Create + seed** — `organizations`; one bootstrap row pinned to `id = 1`, name `Bootstrap Clinic`; identity
   sequence restarted at 2 so the next organization cannot collide; `practitioner_memberships`.
2. **Add nullable columns** — `organization_id integer NULL` on `services`, `locations`, `leads`,
   `practitioner_capabilities`, `availability_rules`, `schedule_blocks`, `appointments`, `audit_events`.
3. **Backfill** — `UPDATE … SET organization_id = 1` on all eight (no row skipped, no row rewritten otherwise);
   `INSERT INTO practitioner_memberships SELECT 1, p.id FROM practitioners p` (`is_active = true`). Exactly one
   organization exists, so nothing is guessed.
4. **Tighten** — `SET NOT NULL` ×8; plain org FKs ×8; `UNIQUE (organization_id, id)` on locations/services/
   leads/appointments; `services_name_key` dropped and `uq_services_organization_name` created; all 12
   composite FKs; the 4 tenant indexes.
5. **Untouched** — `excl_appointments_confirmed_no_overlap`, `uq_capabilities_practitioner_service_location`,
   every CHECK, every table (nothing dropped/recreated), `0001` unedited.

`downgrade()` is the exact inverse: drop indexes → drop composite FKs → restore the global `services_name_key`
→ drop `(organization_id, id)` uniques → drop org FKs and columns → drop `practitioner_memberships` and
`organizations`. Known limitation, deliberate: if two organizations hold the same service name, restoring the
global `services.name` UNIQUE fails — the forward path is the supported one (§19.1).

`alembic.autogenerate.compare_metadata` between the ORM metadata and a database at `0002` reports
**0 differences**: models and migration describe the same schema.

---

## 7. GiST preservation statement

`excl_appointments_confirmed_no_overlap` is **not dropped, recreated, altered, or referenced** by `0002`;
`git diff app/scheduling/models.py` shows only an added explanatory comment above the unchanged
`ExcludeConstraint(...)`. Live definition after upgrade, asserted byte-for-byte by
`test_gist_exclusion_and_capability_unique_are_unchanged` and by the migration test:

```
EXCLUDE USING gist (practitioner_id WITH =, tstzrange(start_utc, end_utc, '[)'::text) WITH &&)
  WHERE (((state)::text = 'confirmed'::text))
```

`organization_id` is absent from that key on purpose (§9 S1): a practitioner shared by two organizations cannot
be in two chairs at 09:00, so the physical invariant outranks tenant isolation. The overlap **preflight**
(`_availability_inputs`' conflicting-appointment query and `find_available_slots`' appointment read) is
likewise still practitioner-global — no `organization_id` filter was added — so preflight and constraint keep
agreeing and a cross-organization clash surfaces as a clean `409 SLOT_BLOCKED`, never a raw `23P01` (§9 S4,
F-16).

---

## 8. PractitionerMembership behaviour

- **PM1** — one global `practitioners` row may hold memberships in many organizations; no "primary" org.
- **PM2** — capabilities, availability rules, schedule blocks and appointments all reference the membership by
  `(organization_id, practitioner_id)`, so a non-member cannot appear in that org's schedule at DB level.
- **PM3** — two independent flags: `practitioners.is_active` (platform-wide) and
  `practitioner_memberships.is_active` (works for this org). Booking eligibility requires both;
  `_load_active_member` raises `ENTITY_INACTIVE` when either is false.
- **PM4** — memberships are never deleted by any flow (RESTRICT everywhere); offboarding is `is_active = false`.
- **PM5/T5** — an organization resolves practitioners only through its own memberships. A practitioner from
  another organization is reported as `NOT_FOUND`, identical to an id that never existed, so the global table is
  never a tenant read surface.
- **PM6** — no per-organization presentation fields; the membership stores only activity (deferred, §18).
- `create_practitioner` registers the global identity **and** the membership for the acting organization in one
  call, which is how the existing HTTP onboarding flow keeps working unchanged.

---

## 9. Proof tests (`tests/test_tenant_integrity.py`, 41 tests — all PASS)

Every DB proof writes raw SQL, so only PostgreSQL judges it.

| # | Required proof | Test(s) | Result |
|---|---|---|---|
| 1 | Appointment cannot mix org A with another tenant's Location / Service / Lead | `test_appointment_cannot_reference_another_tenants_{service,lead,location}`, `test_appointment_requires_a_tenant` | PASS — rejected by `fk_appointments_organization_{service,lead,location}`; NOT NULL for a tenantless row |
| 2 | Capability cannot mix tenant resources | `test_capability_cannot_mix_service_and_location_of_different_tenants`, `test_capability_cannot_reference_another_tenants_service`, `test_capability_cannot_name_a_non_member_practitioner`, `test_create_capability_refuses_a_cross_tenant_service` | PASS |
| 3 | AvailabilityRule / ScheduleBlock cannot mix org / location | `test_availability_rule_cannot_use_another_tenants_location`, `test_availability_rule_cannot_name_a_non_member_practitioner`, `test_schedule_block_cannot_use_another_tenants_location`, `test_schedule_block_service_refuses_a_cross_tenant_location` | PASS |
| 4 | Same service name across orgs, not twice inside one | `test_same_service_name_allowed_in_two_organizations`, `test_duplicate_service_name_inside_one_organization_is_rejected`, `test_global_service_name_unique_constraint_is_gone` | PASS — `uq_services_organization_name` rejects the duplicate with the app check bypassed; `services_name_key` gone |
| 5 | Practitioner in A **and** B, eligible per org | `test_practitioner_can_hold_memberships_in_two_organizations`, `test_shared_practitioner_is_bookable_in_both_organizations`, `test_availability_rules_are_published_per_organization` | PASS — one global row, two memberships, independent availability |
| 6 | Same practitioner still cannot double-book across A and B | `test_confirmed_appointment_in_one_org_blocks_the_interval_in_the_other` (preflight → `SLOT_BLOCKED`, `details == {}`), `test_cross_organization_overlap_is_rejected_by_the_gist_when_preflight_bypassed` (**SQLSTATE 23P01**), `test_slot_query_hides_an_interval_taken_by_the_other_organization`, `test_cross_organization_conflict_leaks_nothing_through_http` (409, `details == {}`, no other tenant's data in the body) | PASS |

Also proven: `fk_appointments_organization_membership` blocks a non-member practitioner (F-3b);
`uq_practitioner_memberships_org_practitioner`; membership deactivation removes eligibility **in that
organization only** while `practitioners.is_active = false` removes it everywhere (PM3); tenant-scoped reads for
services, leads, eligibility, slot query, and cancel/reschedule (A cannot see or mutate B's appointment);
`audit_events.organization_id` NOT NULL + FK, booking/reschedule/cancel audit rows carrying the acting
organization, and `organization.created` audited against its own id (D7); the bootstrap seam resolving the
default tenant; `organization_id NOT NULL` present on all nine tenant tables and absent from `practitioners`.

---

## 10. How the 174-test suite was kept green

No existing **assertion** was weakened or deleted. Changes were confined to fixtures and two
schema-fact constants:

1. **`conftest.py`** — `organizations` and `practitioner_memberships` added to the truncation list, then
   `_seed_bootstrap_organization()` restores exactly what `0002` seeds (org `id = 1`, identity restarted at 2),
   so every test starts from the migration's ground state.
2. **ORM seed helpers** (`test_booking.py`, `test_cancellation.py`, `test_rescheduling.py`) — rows gain
   `organization_id=ORG` and each seed now inserts the `PractitionerMembership`; the returned `ids` dict gained
   `organization_id`, which flows into `book_appointment(...)` and the direct `Appointment(...)` helpers.
3. **Raw-SQL fixtures** (`test_schema_constraints.py`, `test_booking_invariant.py`, `test_api.py`,
   `test_errors.py`) — inserts gained `organization_id` (and a membership row where a practitioner is
   scheduled). These could not stay literally unchanged: `organization_id` is NOT NULL by design.
4. **Two schema facts updated, not weakened** (`test_migrations.py`): `EXPECTED_TABLES` gained `organizations`
   and `practitioner_memberships`; head revision `0001 → 0002`; FK count `12 → 29`.
5. **Zero changes** to `test_catalog_organization.py`, `test_lead.py`, `test_lead_to_appointment_e2e.py`,
   `test_availability.py`, `test_health.py` — the `resolve_organization_id()` seam made service- and HTTP-level
   tests tenant-correct without touching them. The full Vertical 1 E2E (HTTP-only journey, exact response key
   sets, audit sequence) passes unmodified.

---

## 11. Verification evidence

```
$ .venv/bin/python -m pytest -q
217 passed, 12 warnings in 52.88s
```

174 pre-existing + 41 new tenant-integrity + 2 new migration tests. Real PostgreSQL 15 throughout
(`docker-compose` service `db`, port 5434), no mocks, no SQLite.

Migration cycle on a clean database (`odontoflow_pf1_cycle`, created and dropped for the check):

```
$ alembic upgrade head      →  -> 0001, -> 0002 ; alembic current → 0002 (head)
$ alembic downgrade 0001    →  0002 -> 0001    (tenant columns and both tables gone, services_name_key back)
$ alembic downgrade base    →  0001 -> (base)
$ alembic upgrade head      →  -> 0001, -> 0002
$ compare_metadata(models, db@0002) → 0 differences
```

Backfill over real Vertical 1 data (`test_upgrade_backfills_existing_rows_into_the_bootstrap_organization`):
one row per table inserted at `0001`, upgraded to `0002` → every row `organization_id = 1`, one active
membership per practitioner, zero nullable tenant columns, all §7.2 constraints present, `services_name_key`
gone, GiST definition identical.
`test_downgrade_and_reupgrade_preserve_existing_rows`: `0002 → 0001 → 0002` with no row lost and the tenant
attribution restored.

---

## 12. Prior-vertical regression status

| Behaviour | Status |
|---|---|
| Booking (duration from catalog, capability, availability, 15-min grid, half-open intervals) | unchanged, green |
| Cancellation (state-only transition, interval released, `FOR UPDATE` first) | unchanged, green |
| Rescheduling (same row, self-exclusion, no cancelled twin) | unchanged, green |
| Audit atomicity (exactly one row per success, zero on failure, same transaction) | unchanged, green — plus `organization_id` |
| GiST `23P01` → `409 APPOINTMENT_CONFLICT`; `23505` still unmapped (500) | unchanged, green |
| Booking-only one-shot `40P01` retry; cancel/reschedule excluded | untouched (`app/scheduling/router.py` has a zero-line diff) |
| Error envelope `{"error": {code, message, details}}`, `details == {}` | unchanged (`app/errors.py` untouched) |
| OpenAPI paths and schema names | unchanged (routers/schemas untouched) |
| Concurrency tests (2-thread booking race, reschedule races, cancel-vs-reschedule) | unchanged, green |

## 13. Production invariants preserved

- **A1** — every tenant invariant is a PostgreSQL constraint; application checks only produce clear errors.
- **A2** — booking/cancel/reschedule still own their transaction via `session.begin()` on an idle Session; no
  nesting, no middleware, no new transaction owner.
- **A3** — `record_event` still stages a row in the caller's transaction and never commits.
- **A7** — integer `Identity()` PKs and integer FKs everywhere; no UUID, no `public_id`.
- **A8** — additive migration: no table dropped or recreated, no Vertical 1 row discarded.
- **P5/S1** — practitioner-global overlap protection intact; `organization_id` never added to the GiST key.
- **S3/F-12** — cross-tenant conflicts leak nothing: `details == {}`, no other organization's data in any
  response or message (asserted over HTTP).
- **F-17** — audit tenant attribution written at event time; `record_event` has no `organization_id` default.

---

## 14. Blockers

**None.** PF1 is complete and self-contained.

BLOCKER-1 (§23) was needed only for the bootstrap organization's identity; resolved locally as
`id = 1`, `name = 'Bootstrap Clinic'`, exposed to application code as
`app.tenancy.BOOTSTRAP_ORGANIZATION_ID`. No timezone column was added to `organizations`: `Location` already
owns the operational timezone, and PF1 needs no other org attribute. If PF2/PF3 want an organization-level
default timezone, that is an additive column, not a rework.

---

## 15. Risks / follow-ups

1. **Read scoping is an application duty** (§7.4). Composite FKs prevent cross-tenant *writes*, not
   cross-tenant *reads*. Every tenant read goes through `scoped()` and is covered by a negative test, but a
   future unscoped `session.get(Model, id)` would still read another tenant's row. RLS stays deferred; the
   column it needs exists on every tenant table.
2. **The bootstrap default is a temporary seam.** `resolve_organization_id(None) → 1` is correct only while no
   identity exists. PF3 must replace its body with `ctx.organization_id` and remove the
   `organization_id=None` defaults from the application services, or a future multi-tenant deployment would
   silently act on organization 1.
3. **Downgrade is best-effort** for real multi-tenant data: restoring the global `services.name` UNIQUE fails
   if two organizations share a service name (documented in the migration).
4. **`23505` remains unmapped** at the transport, so the org-scoped duplicate service name still surfaces as
   500 through HTTP — unchanged Vertical 1 behaviour, deferred by §18 on purpose.
5. **`create_organization` has no HTTP surface** and no authorization (none exists yet). PF2 must add the PR7
   invariant (system membership + role assignment created in the same transaction) to it.
6. **Test-database hygiene**: `test_upgrade_from_empty_database_creates_schema` (pre-existing) leaks one
   `odontoflow_test_<hex>` database per run; the new PF1 migration fixture drops its own. Worth a small
   cleanup task.

---

## 16. Recommended next block: PF2 — Principal & Authorization

Dependencies are satisfied: `organizations` exists and `locations.(organization_id, id)` is a referenced key,
which is what `memberships`, `roles` and `role_assignments` need (§21 PF2 dependencies).

PF2 should, per §10–§12 and §21:

1. Migration `0003`: `principals` (closed-set `type` CHECK, `UNIQUE (external_subject)`), `memberships`,
   `permissions` (seeded M7 catalog), `role_permissions`, `roles`, `role_assignments` (three composite FKs,
   MATCH SIMPLE, plus the two partial unique indexes of M4); seed the `system` principal (PR6) and its
   membership + role assignment in the bootstrap organization (PR7).
2. `ExecutionContext` as a frozen value object (type only, X8) and `require_permission(session, ctx, code, *,
   location_id=None)` as the single authorization entry point, evaluated live inside the command's transaction.
3. `PERMISSION_DENIED` (403) added to `app/errors.py` — the first PF block allowed to touch the error contract.
4. Organization creation gains the PR7 invariant in the same transaction as `create_organization`.
5. Service-level grant/deny matrix tests only; no HTTP enforcement (that is PF3).

Reusable from PF1: the composite-FK pattern and naming convention, `app/tenancy.py`'s `scoped()` helper, the
`refused_by_database()` raw-SQL proof helper and the `seed_org()` multi-tenant fixture in
`tests/test_tenant_integrity.py`.
