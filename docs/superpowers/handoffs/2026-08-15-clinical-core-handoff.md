# Clinical Core (Patient / Visit / ServiceExecution) — handoff

Date: 2026-08-15 · Authority: `docs/superpowers/specs/2026-08-14-platform-foundation-design.md` (P10, §7, PF2–PF4), legacy extraction `.audit/accelerator/clinical-legacy.md`
Base: Accelerated Core Sprint commit (`015a03f`) · Suite before: 305 passed

## What shipped

New `app/clinical/` module (migration `0005_clinical_core`, additive; 6 new permission codes seeded and
granted to every `system` role, PR7 pattern):

- **Patient** — org-owned (`organization_id` NOT NULL, T1/P10); `full_name` required; `dni` optional with
  **partial** `UNIQUE(organization_id, dni) WHERE dni IS NOT NULL` (per-org clinic identity, adapted from the
  legacy global unique DNI); `sexo` CHECK M/F/O; `phone`; `birth_date` (single nullable date — the legacy
  year/month/day split is not carried over; partial DOB policy deferred).
- **Visit** — org-owned; composite FKs to patient, appointment (nullable, MATCH SIMPLE), practitioner
  membership, location — cross-tenant links structurally impossible. **Appointment-origin rule**: an
  appointment, when given, must belong to the org and be `confirmed`; practitioner/location are derived from
  it; otherwise (walk-in) both are required. The two modes are mutually exclusive at the schema level (422).
  `started_at` is domain-owned (server default): the visit records actual attendance, not a reservation.
- **ServiceExecution** — belongs to a Visit, references the canonical `Service` via composite FK;
  `UNIQUE(organization_id, visit_id, service_id)` (legacy `UNIQUE(id_consulta, id_servicio)`, tenant-qualified);
  `executed_price` Numeric(12,2) NOT NULL >= 0 — the point-in-time **snapshot** the row owns forever (proven:
  later catalog changes do not move it). Multiple different services per visit work; a duplicate is a stable
  `INVALID_INPUT` 422 both sequentially and under a concurrent race (the unique index settles it — no 500).

Reuse, no duplication: PF1 tenant pattern (composite FKs, `scoped`), PF2 permissions (6 new codes,
`require_permission` inside services, ctx-gated), PF3 `record_event` provenance atomic with each mutation,
PF4 claim-first idempotency on all three creates (shared `claim_receipt`/`settle_receipt` extracted into
`app/idempotency/service.py`, scheduling refactored onto them) with replay rendered from the stored outcome.

## API (20 OpenAPI paths; clinical = 6)

`POST /patients` (201, idempotent) · `GET /patients?search=` · `GET /patients/{id}` ·
`POST /visits` (201, idempotent) · `GET /visits?patient_id=` · `GET /visits/{id}` (detail with executions) ·
`GET /visits/{id}/executions` · `POST /visits/{id}/executions` (201, idempotent).

## Proofs (tests/test_clinical_core.py — 19 tests, real PostgreSQL)

Patient tenant isolation + per-org DNI (app + DB backstop); cross-org patient read = NOT_FOUND; visit from
confirmed appointment derives practitioner/location; cancelled appointment rejected as origin; walk-in
requires both; cross-org appointment/patient links rejected by composite FKs (raw-SQL proofs); execution
links visit+service; **price snapshot survives catalog mutation**; multiple executions per visit; duplicate
rule sequential + **concurrent race (threads + Barrier) → 422** + DB backstop; execution in another visit OK;
cross-tenant execution rejected by FK; visit detail includes executions; permissions denied for all three
commands; audit provenance (org/actor/correlation) for all three creates; PF4 idempotency (replay, one row
each, 3 receipts); HTTP journey (create/replay with `Idempotent-Replay` header/list/detail/executions, 422s).

## Test runs

- Focused: `tests/test_clinical_core.py` → **19 passed** (3× runs, no flake).
- Full suite: `.venv/bin/python -m pytest -q` → **324 passed** (305 + 19), including the migration
  upgrade/downgrade/re-upgrade cycle and the tenant-integrity table enumeration (patients/visits/
  service_executions added).
- Adapted existing tests (expected-set drift only): `test_authorization.py` (catalog now 23 codes),
  `test_lead.py` (`patients` now exists as a distinct entity), `test_migrations.py` (HEAD `0005`),
  `test_tenant_integrity.py` (three new tenant-owned tables), `tests/conftest.py` (truncation list).
- OpenAPI regenerated and verified (20 paths).

## Independent review (ONE read-only DeepSeek V4 Flash via the validated runner)

VERDICT: **PASS**, no blockers; 3 ISSUEs — all repaired in the single allowed repair pass:
1. `executions.read` guarded nothing → added `GET /visits/{id}/executions` (task requires "add/**read**
   ServiceExecution").
2. `VisitCreate` accepted the mutually exclusive origin combination silently → schema `model_validator`
   rejects mixed/missing origin payloads with deterministic 422.
3. Concurrent duplicate execution surfaced raw `23505` → the service now maps that exact constraint to the
   stable `INVALID_INPUT` (legacy "duplicate → 500" defect fixed); race proven with threads + Barrier.

## Out of scope (untouched, per contract)

Odontogram, medical history, treatment plans, Finance (Charge/Payment), Inventory (Product/Consumption),
voice/LLM, WhatsApp, agent runtime, async, event bus, frontend clinical screens.

## Docs

`app/clinical/README.md` (module card: metadata/Purpose/Owns/Inputs-Outputs/Dependencies/Invariants/
Public surface/Data/Tests/Next) · `docs/architecture.md` (module table + idempotency/clinical) ·
`docs/roadmap.md` (PF5 DONE; economic/ops NEXT) · CHANGELOG.

## Risks / notes

- Runtime-created organizations still require `provision_system_access` wiring (PF closure item) — tests use
  it explicitly for second-tenant setups.
- The session-expiry trap (rollback expires instances; attribute access silently autobegins) is documented
  by the tests; a shared fixture helper could centralize it later.
- `executed_price` is the execution-time snapshot captured by the caller until Finance defines pricing
  authority; the economic/ops contract (`.audit/clinical-core/next-economic-ops-contract.md`) owns that
  boundary.

## Clinical Core: CLOSED
