# Platform Readiness — Data Ownership / Tenancy (SCOUT A evidence)

**Scope:** baseline `4086dc1`, 174 tests PASS. READ-ONLY audit of persisted entities and
ID exposure, to inform (not decide) a future multi-tenant `Organization` design.
**Method:** every claim is tagged `[FACT] (file:line, symbol)` / `[INFERENCE]` /
`[OPEN QUESTION]`. No design decision is made in this document.

Files inspected: `app/commercial/models.py`, `app/catalog/models.py`,
`app/organization/models.py`, `app/scheduling/models.py`, `app/audit/models.py`,
`alembic/versions/0001_lead_to_appointment.py`, `app/{commercial,catalog,organization,scheduling}/router.py`,
`app/{commercial,catalog,organization,scheduling,audit}/service.py`, `app/scheduling/{query,availability,schemas}.py`,
`app/{commercial,catalog,organization,scheduling}/schemas.py`, `app/__init__.py`, `app/db.py`,
`docs/superpowers/specs/2026-08-12-lead-to-appointment-design.md`,
`docs/superpowers/handoffs/2026-08-13-task-10-lead-to-appointment-e2e-handoff.md`.

---

## A1. CURRENT ENTITY GRAPH

All eight persisted entities share the same PK pattern: `Identity()` integer PKs, no
org/tenant column anywhere. `grep tenant|organization|org_id|clinic` across `app/**/*.py`
returns only the `organization` *module* name; no tenant concept exists.

| Entity | File | PK | FK columns | UNIQUE | CHECK | Other indexes |
|---|---|---|---|---|---|---|
| Service | `app/catalog/models.py:12` | `id` Integer `Identity()` | — | `name` (global, `:13`) | `ck_services_positive_duration` (`:19`) | — |
| Location | `app/organization/models.py:14` | `id` Integer `Identity()` | — | — | — | — |
| Practitioner | `app/organization/models.py:24` | `id` Integer `Identity()` | — | — | — | — |
| PractitionerCapability | `app/organization/models.py:33` | `id` Integer `Identity()` | `practitioner_id`→practitioners (`:34`), `service_id`→services (`:35`), `location_id`→locations (`:36`); all `ondelete="RESTRICT"` | `uq_capabilities_practitioner_service_location` (practitioner_id, service_id, location_id) (`:44-50`) | — | — |
| Lead | `app/commercial/models.py:14` | `id` Integer `Identity()` | `service_need_id`→services, nullable (`:19`) | — | `ck_leads_acquisition_source` (`:26`), `ck_leads_at_least_one_contact` (`:30`) | — |
| AvailabilityRule | `app/scheduling/models.py:15` | `id` Integer `Identity()` | `practitioner_id` (`:16`), `location_id` (`:17`) | — | `ck_availability_rules_weekday` (`:27`), `ck_availability_rules_interval` (`:28`) | — |
| ScheduleBlock | `app/scheduling/models.py:35` | `id` Integer `Identity()` | `practitioner_id` (`:36`), `location_id` (`:37`) | — | `ck_schedule_blocks_interval` (`:46`) | — |
| Appointment | `app/scheduling/models.py:53` | `id` Integer `Identity()` | `lead_id` (`:54`), `service_id` (`:55`), `practitioner_id` (`:56`), `location_id` (`:57`); all `ondelete="RESTRICT"` | — | `ck_appointments_state` (`:69`), `ck_appointments_interval` (`:70`) | partial GiST exclusion `excl_appointments_confirmed_no_overlap` (`:73-78`) |
| AuditEvent | `app/audit/models.py:14` | `id` Integer `Identity()` | — (no FKs) | — | — | `ix_audit_events_entity` (entity_type, entity_id) (`:12`) |

- [FACT] All PKs are `Identity()` integers, including audit: `app/audit/models.py:14`, migration `alembic/versions/0001_lead_to_appointment.py:23,33,42,50,66,86,98,110,131`.
- [FACT] Every FK uses `ondelete="RESTRICT"` (no `CASCADE`, no `SET NULL`), in all model files; the migration mirrors this (`0001...py:51-53,71,87-88,100-101,111-114`).
- [FACT] `Location` has no parent FK and no reference to any other table. It is the **only** organizational grouping; `Practitioner` has no location affiliation of its own except through `PractitionerCapability`/`AvailabilityRule`/`ScheduleBlock`/`Appointment` rows.
- [FACT] `Service` is **global**: it has no FK at all (`app/catalog/models.py:9-20`) and its `name` is globally unique (`:13`). The design doc calls it "catalog" with authoritative duration (`specs/...:56-59`) and does not mention any tenant scope.
- [FACT] `Lead` carries no location/org FK; its only FK is optional `service_need_id` (`app/commercial/models.py:19`).
- [FACT] `AuditEvent` has **zero FKs**; `entity_id` is a polymorphic `String(100)` (`app/audit/models.py:18`), indexed with `entity_type` (`:12`). It cannot be walked back to any tenant-owned row via the schema.
- [FACT] No `Organization`/`Tenant`/`Clinic` entity, column, or table exists in any model or in the single migration (verified by grep and full migration read).
- [INFERENCE] Today "ownership" is expressed only as *location membership*: capabilities, availability, schedule blocks, and appointments all carry `location_id`, while services, leads, practitioners, and audit events are location-less.
- [OPEN QUESTION] Whether `Location` is intended to be the tenant boundary or whether a future `Organization` should sit *above* Location. Nothing in the current schema encodes this.

### Compact FK / ownership graph

```
                 (global, no parent)              (global, no parent)
  services ───────────────────────────────        practitioners ──────────────
     ▲ ▲      ▲                                 ▲   ▲   ▲   ▲   ▲   ▲
     │ │      │            service_need_id      │   │   │   │   │   │
     │ │      └── (nullable) ──► leads ──┐       │   │   │   │   │   │
     │ │                                   │     │   │   │   │   │   │
     │ └── service_id ──► practitioner_capabilities (practitioner_id, service_id, location_id)
     │                ┌───────────▲───────────▲───────────┘
     │                │           │           │
     │                │           │           │
     │                │           │           │        practitioner_id / location_id
     │                │           │           │      ┌─────────────────────────┐
     │                │           │           │      ▼                         ▼
     └── service_id ──► appointments  ◄── lead_id ──┘   availability_rules ──► locations ◄── location_id
                        ▲  location_id ─────────────────┘
                        │  practitioner_id ─────► schedule_blocks ─► locations
                        │                                       ▲  location_id
                        └── location_id ────────────────────────┘

  audit_events ── (no FKs) ──► entity_type/entity_id (polymorphic STRING, indexed)

  Legend: ─► = FK (all ondelete RESTRICT)   ── = no relation    □ location_id = sole grouping key
```

- [FACT] `locations` is the only node with children but no parent; every scheduling/booking table hangs off it via `location_id`.
- [FACT] `services`, `practitioners`, `leads`, `audit_events` are disconnected from the location subtree at the schema level (services/leads only through join of `appointments`, practitioners only through capability/rule/block/appointment rows).
- [FACT] The GiST exclusion is scoped to `practitioner_id` only (`app/scheduling/models.py:74`), deliberately ignoring location; the preflight mirrors this ("Practitioner-wide, not location-scoped", `app/scheduling/query.py:141-143`, `app/scheduling/service.py:111-113`).

---

## A2. MULTI-TENANT BLAST RADIUS (assuming future `Organization` → many `Locations`)

For each entity: would it need DIRECT `organization_id` or DERIVED ownership? Facts, alternatives, consequences. **No decision made.**

### Service
- [FACT] Currently global: no FK, globally-unique `name` (`app/catalog/models.py:13`); `list_services` returns everything (`app/catalog/service.py:24-25`); `create_service` rejects duplicate name globally (`:10-12`).
- [FACT] Referenced by `leads.service_need_id`, `practitioner_capabilities.service_id`, `appointments.service_id` (`app/commercial/models.py:19`, `app/organization/models.py:35`, `app/scheduling/models.py:55`).
- [INFERENCE] If two orgs each want their own "Evaluacion Inicial", the current global `name` UNIQUE blocks the second org. Either `Service` gains direct `organization_id` (per-org catalogs) or it stays global as a shared master catalog that orgs *select* from.
- [CONSEQUENCE] Choosing "global master catalog" avoids a schema change to `services` but pushes tenancy onto the join point (capability) and makes org-specific service *naming/pricing* impossible. Choosing "per-org catalog" requires a new column plus `(organization_id, name)` UNIQUE and per-org filtering in `list_services`.
- [OPEN QUESTION] Is a service shared infrastructure (like a dental-procedure ontology) or org-owned product data? Current schema silently assumes the former.

### Practitioner
- [FACT] `practitioners` has no FK, no org column, and no junction table to any grouping (`app/organization/models.py:21-27`). Membership in a location exists only as rows in `practitioner_capabilities` (and availability/blocks/appointments).
- [FACT] Practitioner-to-location is expressed **only** through `PractitionerCapability` rows (`app/organization/models.py:30-51`); `list_eligible_practitioners` derives eligibility via the capability join (`app/organization/service.py:72-85`).
- [INFERENCE] There is no structural support for a practitioner working at multiple organizations. If a practitioner must belong to org A *and* org B, either (a) `Practitioner` gains direct `organization_id` (one org per practitioner — shared practitioners across orgs become impossible without duplication), (b) a `practitioner_organizations` junction table is introduced (the only structure that makes a practitioner genuinely multi-org), or (c) org membership continues to be implied by capability rows, which today can already point the *same* practitioner at *multiple* locations (already exercised by design: two locations, two practitioners, `handoffs/...:51-54`).
- [CONSEQUENCE] Because `PractitionerCapability` already has `(practitioner_id, service_id, location_id)` globally unique and location is the finest scope, cross-org practitioner *sharing* is technically expressible today with zero schema change — but nothing distinguishes org boundaries, so it is indistinguishable from a bug.

### Lead
- [FACT] `leads` has no location/org FK (`app/commercial/models.py:14-21`); `GET /leads/{lead_id}` fetches by global id (`app/commercial/router.py:18-20`, `app/commercial/service.py:72-76`).
- [FACT] A lead's only tenancy link today is **derived** through `appointments.lead_id` → `appointments.location_id` (`app/scheduling/models.py:54,57`); the design doc states "Every appointment belongs to one location" (`specs/...:64`) but says nothing about leads.
- [INFERENCE] The moment `Lead` becomes tenant-scoped (or is linked to `Patient`, which will presumably be tenant-scoped — see A5), the only derivation path is an appointment that may not exist yet (lead before booking) or may be cancelled. A lead with zero appointments has **no** derivable org.
- [OPEN QUESTION] Should a lead carry an explicit org/location at creation time? Current API `LeadCreate` (`app/commercial/schemas.py:6-11`) has no such field.

### Appointment
- [FACT] `Appointment` is the only entity that references `lead`, `service`, `practitioner`, **and** `location` simultaneously (`app/scheduling/models.py:54-57`). It is the most tenant-complete row in the schema.
- [INFERENCE] `organization_id` on `Appointment` would be **redundant with `location_id`** *only if* each location belongs to exactly one org. If org membership must be queryable on appointments directly (tenant-filtered reads) or if a location could ever map to multiple orgs, direct ownership or a join becomes necessary.
- [CONSEQUENCE] Every existing service/query touches `Appointment` (booking, cancel, reschedule, slot query preflight: `app/scheduling/service.py`, `app/scheduling/query.py:144-153`), so a schema change here has the widest code blast radius of any table.

### AvailabilityRule / ScheduleBlock
- [FACT] Both carry `practitioner_id` + `location_id` (`app/scheduling/models.py:16-17,36-37`).
- [INFERENCE] Ownership is fully derivable from `location_id` (assuming single-org locations). They are structurally the *cheapest* to tenant-scope: no change needed if tenancy is enforced by scoping `location_id` reads, or one `organization_id` column if direct ownership is preferred.
- [CONSEQUENCE] Both are consumed only inside location-scoped queries (`app/scheduling/query.py:123-140`, `app/scheduling/service.py:93-110`), so tenant isolation is almost entirely a *query-scoping* exercise for these tables.

### AuditEvent
- [FACT] No FK, no tenant column; `entity_id` is `String(100)` (`app/audit/models.py:18`), written as `str(appointment.id)` by the scheduling service (`app/scheduling/service.py:202,290,379`).
- [INFERENCE] This is the hardest entity to tenant-scope retroactively: there is no FK to walk and `entity_id` is polymorphic. Tenant filtering would require either a new `organization_id` column written at event time, or parsing/deriving org from `after_state` JSONB (fragile), or a join through a tenant-aware entity table keyed by `(entity_type, entity_id)`.
- [CONSEQUENCE] If an org boundary is not captured *when the event is written*, audit history before the change is not reliably attributable to a tenant.
- [OPEN QUESTION] Should `actor_id`/`correlation_id` (currently defaulting to `"system"`, `app/audit/service.py:15-16,33-34`) be the hook for tenancy instead of an explicit column? The handoff explicitly lists wiring actor/correlation as next-step work (`handoffs/...:190,205-208`).

---

## A3. CURRENT CONSTRAINT IMPACT

### `services.name` UNIQUE (global)
- [FACT] `name` is `unique=True` (`app/catalog/models.py:13`); enforced as column UNIQUE in migration (`0001...py:24`); duplicated at app layer in `create_service` (`app/catalog/service.py:10-12`).
- [INFERENCE] Under a per-org catalog, the constraint must become `(organization_id, name)` — today's global UNIQUE is the single constraint that **will** throw `IntegrityError`/`AppError INVALID_INPUT` on cross-org name collisions the moment two orgs register the same procedure. It is not "naturally separated" by any globally-unique FK because `services` has no parent at all.
- [INFERENCE] Under a shared-global-catalog model, the constraint stays valid and becomes the source of cross-org *consistency* rather than isolation.

### `uq_capabilities_practitioner_service_location`
- [FACT] `UniqueConstraint("practitioner_id", "service_id", "location_id")` (`app/organization/models.py:44-50`).
- [INFERENCE] This uniqueness **is already naturally separated** by the global uniqueness of each FK: `practitioner_id`, `service_id`, and `location_id` are each globally unique integer PKs, so the triple is globally unique regardless of any org column. Adding `organization_id` to the constraint would be *redundant for uniqueness*; the real question is only whether capability rows need an org column for **query scoping / enforcement**, not for constraint correctness.
- [CONSEQUENCE] If `practitioner` and `service` are shared across orgs but `location` is org-scoped, the existing triple still prevents two capabilities for the same practitioner+service at the same location even if that location is shared — a correctness property that survives tenancy unchanged.

### Appointments partial GiST exclusion `(practitioner_id =, tstzrange(...) &&) WHERE state='confirmed'`
- [FACT] `ExcludeConstraint(... practitioner_id =, tstzrange && ... where state='confirmed')` (`app/scheduling/models.py:73-78`); created in migration (`0001...py:121-126`); requires `btree_gist` (`:19`).
- [FACT] The constraint is deliberately **location-agnostic**: the code comment and preflight both state it is "practitioner-wide, not location-scoped" (`app/scheduling/models.py:71-78`, `app/scheduling/query.py:141-143`, `app/scheduling/service.py:111-113`).
- [INFERENCE] Because `practitioner_id` is a globally unique FK, the exclusion already prevents cross-org double booking **without any schema change**. This is a *feature* if a practitioner is single-org (no two orgs can silently double-book the same person), but a *liability* if multi-org practitioners are ever allowed: org A's confirmed appointment would block org B from booking the same practitioner even when org B has no visibility of org A.
- [CONSEQUENCE] Two paths diverge from here and both are consistent with the current schema: (a) keep the global practitioner exclusion and make multi-org practitioners structurally impossible (per-org `practitioners` rows or a junction with a single active org), or (b) add `organization_id` to the exclusion key — a change to the GiST index, the `btree_gist` columns, and every preflight overlap query.
- [OPEN QUESTION] Is "a practitioner cannot be double-booked across locations/orgs" a hard domain invariant (current behavior) or only a within-org invariant (what multi-org would need)?

### CHECK constraints
- [FACT] All CHECKs are value-domain constraints: acquisition source enum (`app/commercial/models.py:26-33`), positive duration (`app/catalog/models.py:19`), weekday/interval bounds (`app/scheduling/models.py:27-28,46`), appointment state + interval (`:69-70`).
- [INFERENCE] None of them reference org/tenant scope and none need to change for tenancy. The `state` CHECK is the only one near the tenancy question because the GiST exclusion is *predicated on* `state='confirmed'`.

### Indexes
- [FACT] Only explicit index is `ix_audit_events_entity (entity_type, entity_id)` (`app/audit/models.py:12`).
- [INFERENCE] A future tenant-filtered scan of audit/history would likely need an org-scoped index; nothing today would conflict, but nothing helps either.

---

## A4. ID STRATEGY

### PK / public-identifier inventory
- [FACT] Every PK is `Identity()` integer (`A1` table, `0001...py:23-131`). No natural keys used as PKs.
- [FACT] The only *non-PK* identifier treated as a durable reference is `AuditEvent.entity_id` (`String(100)`, `app/audit/models.py:18`), written as `str(appointment.id)` in all three lifecycle writes (`app/scheduling/service.py:202,290,379`).

### Where IDs are exposed through FastAPI routes (FACT citations)
- [FACT] Path param: `GET /leads/{lead_id}` with `lead_id: int` (`app/commercial/router.py:18-20`).
- [FACT] Path param: `POST /appointments/{appointment_id}/cancel` and `/reschedule`, both `appointment_id: int` (`app/scheduling/router.py:142-157`).
- [FACT] Query param: `GET /practitioners/eligible?service_id=&location_id=`, both `int` (`app/organization/router.py:44-48`).
- [FACT] Request bodies: `SlotQuery.service_id/location_id` (`app/scheduling/schemas.py:42-47`), `AppointmentCreate.lead_id/service_id/location_id/practitioner_id` (`:55-63`), `AppointmentReschedule` (no id), `CapabilityCreate.practitioner_id/service_id/location_id` (`app/organization/schemas.py:31-35`), `LeadCreate.service_need_id` (`app/commercial/schemas.py:11`).
- [FACT] Response bodies: every Read schema returns `id: int` (`app/commercial/schemas.py:17`, `app/catalog/schemas.py:13`, `app/organization/schemas.py:12,25,41`, `app/scheduling/schemas.py:17,35,82`); `SlotResult` returns `practitioner_id: int` (`:49-52`); the slot query is served to clients as `POST /slots/query` returning practitioner ids (`app/scheduling/query.py:163-172`).

### Sortable / sequential assumptions
- [FACT] `list_services` orders by `name`, not id (`app/catalog/service.py:25`); eligible practitioners ordered by `display_name` (`app/organization/service.py:84`); slots sorted by `(start, end, practitioner_id)` (`app/scheduling/query.py:172`) and chronologically inside the engine (`app/scheduling/availability.py:159`). **No production ordering depends on integer id.**
- [FACT] The E2E test asserts audit ordering "guaranteed by the monotonic id of the sequence" (`handoffs/...:138`) and queries audit rows `.order_by(AuditEvent.id)` (`tests/test_lead_to_appointment_e2e.py:406`); appointments also read `.order_by(Appointment.id)` in that test (`:383`).
- [INFERENCE] Sequential-ordering reliance exists **only in tests and the handoff narrative**, not in application code. It would survive a UUID conversion only if the tests stop relying on id order and instead use `occurred_at`/`correlation_id` or a monotonic secondary.
- [INFERENCE] `appointments.id` is also used as a **sort/tiebreak** in `SlotResult` ordering? No — slot ordering uses `practitioner_id` as tiebreak after time (`app/scheduling/query.py:172`), not appointment ids.

### What would actually break if PKs stayed integers
- [FACT/NEGATIVE] Nothing in the current code breaks with integer PKs; all FK/route/schema/audit paths are typed for `int`. This is the zero-cost baseline.
- [INFERENCE] The risk of keeping integers is *not* code-breaking today; it is (a) enumeration of tenant entity ids via unauthenticated... (no auth exists), and (b) cross-tenant id confusion if a single API serves multiple orgs and id-guessing becomes an information-leak vector. Current API has **no authentication** (no auth dependency in any router; `handoffs/...:190` defers the role model), so integer ids are fully enumerable today.
- [OPEN QUESTION] Whether id enumeration matters before authentication exists.

### What would actually break if a public UUID/ULID were added later
- [FACT] Route/schema signatures typed `int` would reject UUID strings with `422` before the handler runs: `lead_id: int` (`app/commercial/router.py:19`), `appointment_id: int` (`app/scheduling/router.py:144,153`), `service_id/location_id: int` (`app/organization/router.py:46`).
- [FACT] `app.scheduling.query.py:38-39` and `app.scheduling.service.py:58-59` accept `entity_id: int` and call `session.get(model, entity_id)`; service-level APIs are typed int throughout (`book_appointment(... lead_id: int ...)`, `app/scheduling/service.py:133-136`).
- [FACT] Audit write path converts to string (`str(appointment.id)`, `app/scheduling/service.py:202,290,379`) into a `String(100)` column — a UUID/ULID would *fit* the column, so `audit_events` is the **one place already UUID-tolerant**.
- [INFERENCE] A later public-UUID conversion would touch: all 8 model PKs + every FK column type (or keep int PKs and add a separate `uuid`/`public_id` column), every Pydantic schema (`int` → `UUID`/`str`), every route signature, every service signature, the audit `entity_id` writer, every test fixture (tests insert raw ints and build references: `tests/test_lead_to_appointment_e2e.py`, `test_api.py`, `test_booking.py`, etc.), and the reschedule self-exclusion predicate `Appointment.id != exclude_appointment_id` (`app/scheduling/service.py:124`). The **least invasive** alternative is adding a `public_id` column while keeping the integer PK as the FK/constraint spine.
- [CONSEQUENCE] Converting the PK itself to UUID makes every FK wider and every GiST/exclusion row larger; keeping `int` PK + separate public identifier keeps the constraint spine intact and confines changes to the API surface and audit string.

### Blast radius summary (FACT-driven inventory)
- Routers: 4 files, ~7 signatures (`app/{commercial,catalog,organization,scheduling}/router.py`).
- Schemas: 4 files, 12+ `int` fields (`app/*/schemas.py`).
- Services/query: `app/scheduling/service.py` (5 int params + `_load_active`), `app/scheduling/query.py:38,51-97,99-172`, `app/catalog/service.py:9-12,24-25`, `app/commercial/service.py:36-76`, `app/organization/service.py:37-85`.
- Audit: writer already string-typed (`app/audit/service.py:23,36`); only the *source* (`str(appointment.id)`) changes.

---

## A5. FUTURE DOMAIN RISK (if `Organization` ownership is introduced AFTER Patient, Visit, ServiceExecution, Charge, Payment, InventoryMovement exist)

Based **only** on the current schema — these entities are not designed here.

- [FACT] The current schema has **no Patient**; the only person-anchor is `Lead`, which carries **no location/org FK** (`app/commercial/models.py:14-21`). Its sole derivable tenancy link is `appointments.lead_id → location_id` (`app/scheduling/models.py:54,57`), which requires an existing appointment.
- [INFERENCE] If `Patient` and the financial chain (`Charge`, `Payment`) attach to `Lead` (the natural anchor), they inherit a tenant anchor that is **empty for unbooked leads**. If they attach to `Appointment`, they inherit `location_id` but only for booked patients — a pre-appointment consultation or a payment recorded before/without a booking would have no org. Either way, the org derivation for the entire commercial/financial tree depends on a single optional FK chain that can be absent.
- [INFERENCE] `ServiceExecution` and `Charge` would reference `services` (global, no FK to any grouping, `app/catalog/models.py:9-20`) and a practitioner/location. Under the current schema the org scope would come from the visit's location FK, so every future visit/execution must be created **with** a location FK or the org is underivable.
- [INFERENCE] `InventoryMovement` has no ancestor at all in the current graph; nothing constrains where it would hang. If it is introduced without an explicit location/org column (e.g., attached to a `ServiceExecution` or a product), its tenant scope would depend entirely on whatever path the designer chooses.
- [INFERENCE] The `AuditEvent` gap compounds: since `entity_id` is a polymorphic string with no FK (`app/audit/models.py:18`), auditing future `Patient`/`Charge`/`Payment` events without capturing an org at write time makes tenant-scoped audit history *unrecoverable* after the fact (same root issue as A2·AuditEvent).
- [CONSEQUENCE] The pattern that makes retrofitting hard is consistent: **every future entity's org scope would be derived through a location FK**, and the only entities that reliably carry one today are `Appointment`, `AvailabilityRule`, `ScheduleBlock`, `PractitionerCapability`. `Lead`, `Service`, `AuditEvent` — the roots of the commercial, catalog, and audit trees — do not, and would each need a migration adding an org column (and, for audit, backfilled attribution).

---

## TOP-5 RISKS for later `Organization` introduction

1. **[HIGH] Lead (and any future Patient/Charge/Payment) has no tenancy anchor** — `app/commercial/models.py:14-21`. Org scope is derivable only through an existing `Appointment`; a never-booked or pre-booking lead, and any financial/clinical row attached to it, is underivable. Data created before the migration cannot be attributed to a tenant.
2. **[HIGH] `services.name` global UNIQUE** — `app/catalog/models.py:13`. The first two orgs registering the same procedure collide; today's constraint (and the app-layer duplicate check, `app/catalog/service.py:10-12`) must become org-scoped or the catalog must be declared a shared master.
3. **[HIGH] Audit history is not tenant-attributable** — `app/audit/models.py:12-18`. No FK, polymorphic `entity_id`, org not captured at write time (`app/scheduling/service.py:202,290,379`). Retroactive tenant attribution of audit_events is impossible without a backfill that can only guess from `after_state` JSONB.
4. **[MEDIUM] GiST exclusion is practitioner-global** — `app/scheduling/models.py:73-78` + preflight (`app/scheduling/query.py:141-143`, `app/scheduling/service.py:111-113`). Correct today, it silently *prevents* multi-org practitioners later; adding org to the exclusion is a schema+index+query change, and the overlap queries are the widest-touching code path.
5. **[MEDIUM] Public integer IDs with no authentication** — no auth dependency in any router (`app/*/router.py`); ids exposed as ints in paths, query params, bodies, and responses (A4). Cross-tenant enumeration/id-confusion becomes a live risk exactly when the API starts serving more than one org.

---

## Appendices

- [FACT] Baseline: `git log` head `4086dc1 test: close lead-to-appointment vertical with e2e`; working tree clean; single migration `0001_lead_to_appointment.py` defines the whole schema; handoff reports 174 tests PASS (`handoffs/...:163`).
- [FACT] The handoff's stated next step is exactly this gate: "Platform Readiness Gate — Multi-Tenant & Agent-Native Foundation … aislar organización/sede por tenant, cablear actor y correlation id" (`handoffs/...:205-208`).
- [OPEN QUESTION] Intended tenant granularity: `Organization` per practice, or per location, or a chain with shared practitioners/services? The schema does not disambiguate, and every alternative in A2/A3 has different constraint and query consequences.
- [OPEN QUESTION] Should `Practitioner` be multi-org? The GiST exclusion (A3) and the absence of any junction table (A2) currently make single-org the structurally consistent assumption.
