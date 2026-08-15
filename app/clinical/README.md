# app/clinical — Clinical Core

**Metadata:** module `clinical` · migration `0005_clinical_core` · PF5 · owns Patient, Visit, ServiceExecution.

## Purpose

The first clinical vertical: an organization-owned patient record, the attended
encounter (Visit) that realizes a confirmed appointment (or a walk-in), and the
executed service lines (ServiceExecution) with their point-in-time price
snapshot. This is the anchor every future Finance/Inventory vertical attaches
to.

## Owns

- `Patient` — durable clinic identity, per-organization DNI.
- `Visit` — attended clinical encounter (not a reservation).
- `ServiceExecution` — one service actually performed during a visit.

## Inputs / Outputs

- **Inputs:** HTTP (routers) → Pydantic schemas (`extra="forbid"`) → application
  services with an explicit `ExecutionContext` and an idle `Session`.
- **Outputs:** typed read DTOs (`PatientRead`, `VisitRead`, `VisitDetailRead`,
  `ServiceExecutionRead`); commands return the domain objects, the router renders.

## Dependencies

- `app/iam` — permissions (`patients.*`, `visits.*`, `executions.*`) and `require_permission` (PF2).
- `app/idempotency` — `claim_receipt`/`settle_receipt`/`run_idempotent_command` (PF4).
- `app/audit` — `record_event` (PF3 provenance, atomic with the mutation).
- `app/scheduling` — `Appointment` (confirmed-origin rule).
- `app/catalog` — canonical `Service` (execution lines reference it).
- `app/organization` — `Location`, `Practitioner` membership.
- `app/tenancy` — `scoped` tenant filtering.

## Invariants

- Every table carries `organization_id` NOT NULL; cross-tenant relationships are
  structurally impossible (composite FKs, §7 pattern).
- `patients`: partial `UNIQUE(organization_id, dni) WHERE dni IS NOT NULL`;
  `sexo IN ('M','F','O')`.
- `visits`: composite FKs to patient, appointment (nullable, MATCH SIMPLE),
  practitioner membership, location; `started_at` domain-owned.
- `service_executions`: `UNIQUE(organization_id, visit_id, service_id)` (a
  service executes at most once per visit); `executed_price` Numeric(12,2) NOT
  NULL >= 0 — a point-in-time snapshot the row owns forever.
- All FKs `RESTRICT`; integer Identity PKs; timestamptz.

## Public surface

| Method | Path | Permission | Idempotency |
|---|---|---|---|
| POST | `/patients` | `patients.create` | Yes |
| GET | `/patients?search=` · `/patients/{id}` | `patients.read` | — |
| POST | `/visits` | `visits.create` | Yes |
| GET | `/visits?patient_id=` · `/visits/{id}` | `visits.read` | — |
| POST | `/visits/{id}/executions` | `executions.create` | Yes |
| GET | `/visits/{id}/executions` | `executions.read` | — |

## Data

`patients` → `visits` (1:N) → `service_executions` (1:N). Visit→Appointment 0..1.
Legacy mapping: Paciente→Patient, Consulta→Visit, ConsultaServicio→ServiceExecution
(see `.audit/accelerator/clinical-legacy.md` for the extraction).

## Tests

`tests/test_clinical_core.py` (19 tests): tenant isolation at service and DB
level, appointment-origin rules, walk-in rule, cross-tenant composite-FK
rejections, price-snapshot immutability, multi-execution + duplicate rule
(sequential, concurrent race, DB backstop), permissions, audit provenance, PF4
idempotency + replay, HTTP journey, executions list endpoint.

## Next

- Clinical Bridge follow-ups per `docs/superpowers/handoffs/2026-08-15-clinical-core-handoff.md`:
  visit completion states, odontogram/history (deferred), and the economic/ops
  contract (`ServiceExecution → ServiceConsumption → Product`, `ServiceExecution
  → Charge → Payment`) defined in `.audit/clinical-core/next-economic-ops-contract.md`.
