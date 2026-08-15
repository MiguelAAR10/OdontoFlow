# Accelerated Core Sprint — handoff

Date: 2026-08-15 · Authority: backend `docs/superpowers/specs/2026-08-14-platform-foundation-design.md`, integration contract `docs/integration/frontend-backend-contract.md`
Workspace: `~/projects/portfolio/odontoflow/` (odontosmart-backend + odontosmart-frontend, separate repos, not merged)

## What shipped

**Backend (odontosmart-backend).** Four read endpoints that make the Agenda real, on the PF1–PF4 spine:
- `GET /appointments` — org-scoped, half-open `[from_date, to_date)` window, `location_id`/`practitioner_id` filters, joined display names (`AppointmentListItem`), `appointments.read` (location-scoped grants honored).
- `GET /appointments/{id}` — tenant-scoped (cross-org id → `NOT_FOUND`, E8); permission vs the appointment's own location.
- `GET /leads` — `search` (name/phone ilike) + `commercial_status`, `leads.read`.
- `GET /locations` — org list, `locations.read`.
- OpenAPI regenerated (19 operations). 305 tests green (294 prior + 11 new in `tests/test_integration_reads.py`).

**Frontend (odontosmart-frontend).** Agenda wired to the real API with the visual design kept:
- `src/contracts/api.ts` — OpenAPI-generated TypeScript (openapi-typescript), no handwritten API models.
- `src/contracts/client.ts` — typed client + `ApiError` envelope mapping + `Idempotency-Key` on book/reschedule/cancel.
- `src/api.ts` — view-model adapters (`toGridSlot` Lima tz, `toUiStatus`, `currentWeekWindow`); `VITE_USE_MOCKS=true` unchanged for every other screen.
- `AgendaPage` + `AppShell` real mode: selectors from real leads/services/locations/eligible practitioners, booking/reschedule/cancel with per-intent idempotency keys, error banner mapping.
- Tests: `test/agenda-adapter.test.ts` (unit) + `test/agenda-integration.test.ts` (E2E, excluded from default run, `npm run test:e2e:agenda`).

## Proofs

- Backend focused: `tests/test_integration_reads.py` → **11 passed**; full `pytest -q` → **305 passed** (no PF1–PF4 regression).
- Frontend: `npm run typecheck` clean · `npm test` **31 passed** · `npm run build` OK.
- **Agenda E2E (React adapter → FastAPI → PostgreSQL, `VITE_USE_MOCKS=false`):** real slots list → book → same-key replay (exactly-once, same id) → list shows exactly one → reload → reschedule → reload → cancel → reload cancelled. **3/3 passed against a live uvicorn on `odontoflow_test`**; no mockData in this path (by construction: the module under test is the real-mode adapter).
- No Patient/Finance/Inventory/Chat code added to the backend; the Node simulator was not copied.

## Legacy evidence (read-only scouts, `.audit/accelerator/`)

- `clinical-legacy.md` (Scout A): Paciente/Consulta/ConsultaServicio/ServicioCatalogo — meaning, fields, invariants (`UNIQUE(id_consulta, id_servicio)`, DNI unique, price snapshot), lifecycle, PRICE behavior, edge cases, coupling to drop, PRESERVE/ADAPT/DEFER/DROP table.
- `ops-finance-legacy.md` (Scout B): Producto/ConsumoProducto/movimientos/Pago/Factura/MedioPago — consumable-vs-sellable is **inferred, never declared**; consumption always anchored to a service line; purchase/ENTRADA path is DB-only (SP never called); **no transfer/location semantics exist**; factura 1:1 consulta + pagos N:1; **six stock-write authorities** with two bypassing the ledger (defect to DROP); dead `inventario_bp`; schema typos; ledger-trigger design intent to preserve.
- Both reports: `.audit/accelerator/{clinical-legacy,ops-finance-legacy}.md` (persisted evidence).

## Next vertical proposal — Clinical Bridge (Appointment → Patient → Visit → ServiceExecution → ServiceConsumption/Charge boundary)

**Do NOT implement yet** — this is the approved next activity's design input, synthesized from the legacy evidence.

- **Preserved from legacy:** DNI-based patient identity (per-org unique, partial DOB tolerated, M/F/O, optional phone); visit header with free notes and multiple executed service lines; `UNIQUE(visit, service)` (one execution per service per visit); price/catalog snapshot at execution; incremental attachment of services to a visit; patient-before-visit creation order.
- **Adapted:** Consulta (day-granular, no practitioner/location) → `Visit` (instant, appointment-linked, org/practitioner/location-scoped); ConsultaServicio → `ServiceExecution` (org-scoped composite FKs to canonical `services`); DNI global-unique → per-org partial unique; creation flows → PF3-audited, PF4-idempotent commands; service lines attach via idempotent add-command.
- **Dropped:** hard DELETE + cascade-orphan history; `total_historico` on the header; `paciente_problematico`; `id_distrito`; duplicate `Servicio` model; day-only granularity; the dead inventory route; the six-authority stock writes; invoice coupling on the visit (Finance).
- **PostgreSQL ownership/invariants:** `patients` (`organization_id` NOT NULL, partial `UNIQUE(organization_id, dni) WHERE dni IS NOT NULL`, `uq_patients_organization_id`); `visits` (composite FKs `(org, patient)`, `(org, appointment)`, `(org, practitioner)`, `(org, location)`, state CHECK); `service_executions` (`UNIQUE(organization_id, visit_id, service_id)`, composite FKs `(org, visit)`, `(org, service)`); all RESTRICT, integer Identity PKs, timestamptz.
- **Transaction boundary:** one command = one `session.begin()` (A2): create Patient, create Visit (claim-first idempotency), add ServiceExecution (idempotent), each with atomic audit; replay read in a separate transaction (PF4 pattern).
- **Minimum API needed:** `POST /patients`, `GET /patients` (+search), `POST /visits` (from a confirmed appointment), `GET /visits`, `POST /visits/{id}/executions`, `GET /visits/{id}` — all with PF2 permission codes (`patients.*`, `visits.*` added to the catalog) and PF3 ctx.
- **Dependencies on future Finance/Inventory:** the execution line is the anchor both verticals attach to; price/duration snapshots and `ServiceConsumption`/`Charge` boundary are Finance/Inventory-owned and deferred — the Clinical Bridge must expose the execution line id but not implement either.
- **Legacy tests:** none exist; the seed SQL (`03-07_insert_*.sql`) and the DDL invariants become characterization fixtures.

## Docs

- `docs/integration/frontend-backend-contract.md` — updated: 19-operation contract, matrix rows for the implemented vertical marked done, gaps section rewritten.
- `docs/roadmap.md` — DONE/NOW/NEXT updated (reads + Agenda vertical closed; Clinical Bridge proposal noted).
- `docs/integration/frontend-current-state.md`, `module-integration-map.md`, `data-flow.md` — unchanged (still accurate; current-state is a historical snapshot).
- Baseline + scout evidence: `.audit/accelerator/baseline.md`, `clinical-legacy.md`, `ops-finance-legacy.md`, `seed_e2e.py`.

## Risks / notes

- The agenda E2E books slots returned by `slots/query` (self-healing across runs); leftover data in `odontoflow_test` is reset by the pytest session fixture.
- HTTP identity is still the PF3 default context (system, bootstrap org): reads are permission-checked but the HTTP layer always passes today; the service-level permission tests prove the deny path.
- `docs/superpowers/handoffs/*` received only the mechanical `../medistock` → `../../AI-EdgeRunners/medistock` path fix (STEP 0 mandate).
- Followup states ("Por confirmar"/"No respondió") remain frontend-only; a Followup domain module is a future activity (simulator rules = reference).

## Sprint verdict: PASS (10/10)

1. Repos clean/understood ✓ 2. Backend read API PASS (11 new tests) ✓ 3. Agenda against real FastAPI/PostgreSQL ✓ (E2E 3/3, no mocks) 4. OpenAPI-derived frontend contract ✓ 5. No fake Patient/Finance/Inventory ✓ 6. Clinical legacy evidence ✓ 7. Ops/Finance legacy evidence ✓ 8. Concise docs updated ✓ 9. No PF1–PF4 regression (305 green) ✓ 10. One handoff ✓

## Accelerated Core Sprint: CLOSED
