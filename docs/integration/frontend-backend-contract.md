# Frontend ↔ Backend Contract

HEADs: frontend `8769f12` + Accelerated Core Sprint commit · backend `34cfbf7` + Accelerated Core Sprint commit
(PF1–PF4 CLOSED; 305 tests green)
Status: **first vertical (Agenda ↔ Scheduling) IMPLEMENTED and E2E-proven** — the read endpoints below are
live, the frontend adapter runs the agenda against real FastAPI/PostgreSQL with `VITE_USE_MOCKS=false`.
Authority: FastAPI + PostgreSQL (OdontoFlow) — **the domain authority**. The React visual design is kept; the frontend adapts to the real domain, never the reverse. OpenAPI-derived TypeScript contracts are preferred over duplicated handwritten models.

## Real API contract (OpenAPI `docs/api/openapi.json`, 19 operations)

| Method | Path | Purpose | Idempotency |
|---|---|---|---|
| GET | `/health` | liveness | — |
| GET | `/services` · POST `/services` | catalog | — |
| POST | `/leads` · GET `/leads/{lead_id}` | commercial lead | — |
| **GET** | **`/leads` (`search`, `commercial_status`)** | lead list for selectors/search | — |
| POST | `/locations` · **GET `/locations`** | branch | — |
| POST | `/practitioners` | global practitioner + onboarding | — |
| POST | `/capabilities` | practitioner capability | — |
| GET | `/practitioners/eligible?service_id&location_id` | eligible practitioners | — |
| POST | `/availability-rules` · POST `/schedule-blocks` | schedule config | — |
| POST | `/slots/query` | bookable slots in window | — |
| **GET** | **`/appointments` (`from_date`, `to_date`, `location_id`, `practitioner_id`)** | agenda list (half-open window) | — |
| **GET** | **`/appointments/{id}`** | agenda detail | — |
| POST | `/appointments` (201) | book | **Yes** (`Idempotency-Key`) |
| POST | `/appointments/{id}/cancel` (200) | cancel | **Yes** |
| POST | `/appointments/{id}/reschedule` (200) | reschedule | **Yes** |

Error envelope: `{"error": {"code", "message", "details"}}` with `INVALID_INPUT` 422, `NOT_FOUND` 404, `PERMISSION_DENIED` 403 (service-declared), `ENTITY_INACTIVE|CAPABILITY_MISSING|SLOT_BLOCKED|APPOINTMENT_CONFLICT|IDEMPOTENCY_KEY_REUSED` 409. `23P01`→`APPOINTMENT_CONFLICT`; `40P01`→one-shot retry for booking; never leak SQLSTATE/constraints.

## Integration matrix

| FRONTEND ACTION | CURRENT FRONT CONTRACT | CURRENT FASTAPI CAPABILITY | MATCH/ADAPTER/GAP/FUTURE | REQUIRED CHANGE | AUTHORITATIVE DOMAIN OWNER |
|---|---|---|---|---|---|
| Agenda load (AgendaPage) | `GET /appointments` → `Appointment[] {day,time,patient,…}` | **Implemented**: `GET /appointments` (org-scoped, date-window/location/practitioner filters, `appointments.read`) | **MATCH (done)** | Adapter in `src/api.ts` (`loadAgenda`) + OpenAPI types | Scheduling |
| Agenda detail modal | `GET /appointments` (same list) | **Implemented**: `GET /appointments/{id}` (tenant-scoped NOT_FOUND) | **MATCH (done)** | `getAgendaDetail` + view model | Scheduling |
| Agenda week grid rendering | `day: 0-5`, `time "HH:MM"` (week Mon–Sat 09:00–14:00) | `start_utc/end_utc` datetime | **ADAPTER (done)** | `toGridSlot`/`toUiStatus`/`currentWeekWindow` in `src/api.ts`; React design kept | Scheduling (frontend adapter owned by frontend repo) |
| Agenda branch/status filters | branch enums (Lince/JM/Magdalena); status `Confirmada|Por confirmar|No respondió` | `location_id`; `state: confirmed|cancelled` | **ADAPTER (done) + FUTURE** | Branch options now come from `GET /locations`; `Confirmada`/`Cancelada` mapped from `state`; "Por confirmar/No respondió" are **followup** states → FUTURE (followup module) | Scheduling · Followup (future) |
| Nueva cita (AppShell modal) | `POST /appointments {patient, treatment, doctor, branch, date, time}` | `POST /appointments {lead_id, service_id, location_id, practitioner_id, start}` + `Idempotency-Key` | **ADAPTER (done)** | Real-mode modal: lead/service/location/eligible-practitioner selectors + date/time → start; `Idempotency-Key` per intent; replay-safe | Scheduling · Commercial (Lead) |
| Editar cita (inert button) | (none) | `POST /appointments/{id}/reschedule` (idempotent) | **FUTURE** | Wire the button: reschedule modal → same adapter rules | Scheduling |
| Pacientes list/search/create | `GET|POST /patients` (Patient w/ branch, status Activo/Lead/Pendiente) | **No Patient domain** (deferred); `POST /leads`, `GET /leads/{id}` exist | **ADAPTER** | Re-point the "Pacientes" screen at **Leads** (CRM): add `GET /leads` (list + search name/phone/DNI-adjacent, `leads.read`); map status: Lead/Pendiente/Activo → lead commercial_status (future progression) | Commercial (Lead) · Patient = FUTURE |
| Header patient search | search → `/pacientes?patient=id` | — | **ADAPTER** | Same as Pacientes screen (Lead search) | Commercial |
| Agent dashboard KPIs + automations | hard-coded KPIs; static automations | — | **MOCK_ONLY** | Stays prototype until agent runtime module exists | Agent runtime (future) |
| Human queue / config toggles | local-only state | — | **MOCK_ONLY** | Stays prototype | Agent runtime (future) |
| Caja (Movimientos/close) | `GET|POST /cash/movements`; close local | — | **MOCK_ONLY** | Stays prototype until Finance | Finance (future) |
| Inventario (Productos/Compras) | `GET|POST /inventory/*` | — | **MOCK_ONLY** | Stays prototype until Inventory | Inventory (future) |
| Chat conversations/messages | `GET /conversations`, `POST /conversations/{id}/messages` | — | **MOCK_ONLY** | Stays prototype; never a direct DB writer | Chat/Agent (future) |
| Crear cita desde conversación | hard-coded date/doctor/branch | booking exists | **FUTURE** | Voice/chat → **structured-draft workflow** (frontend drafts → user confirms → booking command w/ idempotency) | Agent runtime (future) |
| Followup cadence (d-1 09:00, calls 12/16, same-day, 1h before) | simulator FollowupEngine rules | not in backend | **FUTURE (REFERENCE)** | Followup module later; simulator rules = reference spec only, never copied code | Followup (future) |

## Backend gaps the frontend needs (read endpoints only; no new domains)

All four first-vertical reads are **implemented** (`GET /appointments`, `GET /appointments/{id}`, `GET /leads`, `GET /locations`). Remaining future surfaces: `GET /availability-rules`, `GET /schedule-blocks`, `GET /audit`, lead status progression — not required by the shipped vertical.

## Boundaries (non-negotiable)

- FastAPI/PostgreSQL remain the domain authority. The frontend **never** mimics mockData shapes where they conflict with the real domain (e.g. `day: 0-5` vs `start_utc`, `status "No respondió"` vs `state`).
- The Node simulator is **not** copied into OdontoFlow; its FollowupEngine/scheduler/simulation behavior is **reference only**.
- PF1 tenancy, PF2 permissions, PF3 provenance, PF4 idempotency are preserved on every new endpoint (tenant-scoped reads, `require_permission` on mutations, `record_event` audit, `Idempotency-Key` on mutations).
- Patient, Finance, Inventory, Chat are **not implemented** to satisfy existing mock screens; those features remain explicitly MOCK/PROTOTYPE until their backend authority exists.
- Voice Assistant is a future structured-draft workflow (frontend draft → user confirmation → idempotent booking command), **never a direct DB writer**.

## First implementation vertical — PROVEN AND IMPLEMENTED: Agenda ↔ Scheduling

Proven by evidence: the SPA's landing route is `/agenda` (App.tsx:14-15); the agenda is the only screen whose primary data (appointments) has a real backend authority. **Implemented in the Accelerated Core Sprint**: the four read endpoints, the frontend adapter (`src/contracts/client.ts` + `src/api.ts` view models), booking/reschedule/cancel with `Idempotency-Key`, and the E2E proof (see the sprint handoff). Remaining rows of the matrix below keep their classification for future work.

### USER FLOW
1. User opens the app → lands on Agenda.
2. Agenda loads the week's appointments (date window) from the backend.
3. User filters by branch (location) and/or status (client-side today).
4. User opens a detail modal → reads appointment by id.
5. User creates an appointment: selects lead (patient), treatment (service), doctor (eligible practitioner), branch (location), date+time → POST `/appointments` with `Idempotency-Key` → 201 → toast + agenda refresh (the existing `appointment-created` window event).
6. (Future) Editar cita → reschedule modal.

### FRONT COMPONENTS
- `AgendaPage.tsx` (grid + filters + detail modal) — keep visual design; replace `getAppointments` data source and `day/time` derivation.
- `AppShell.tsx` "Nueva cita" modal — replace mock save with `createAppointment` adapter (lead/service/practitioner/location selectors).
- `src/api.ts` — new adapter functions against real endpoints; `VITE_USE_MOCKS=false` mode becomes the only path (mock fallback kept for design-time).
- New `src/contracts/*.ts` — OpenAPI-generated types (openapi-typescript) replacing handwritten `types.ts` for the vertical's entities.

### HTTP CONTRACT
- `GET /appointments?from=YYYY-MM-DD&to=YYYY-MM-DD&location_id=&practitioner_id=` → `AppointmentRead[]` (new backend endpoint).
- `GET /appointments/{id}` → `AppointmentRead` (new).
- `GET /leads` (new, list for the selector), `GET /locations` (new), `GET /services` (exists), `GET /practitioners/eligible?service_id&location_id` (exists).
- `POST /appointments` `{lead_id, service_id, location_id, practitioner_id, start}` + `Idempotency-Key` → 201 `AppointmentRead`.
- `GET /slots/query` (exists) for slot picker (optional in vertical 1; date+time direct entry acceptable).

### TYPE MAPPING
| Frontend (UI) | Backend (OpenAPI) | Notes |
|---|---|---|
| `Appointment.day (0-5) + time` | `start_utc/end_utc` (timestamptz) | adapter via location timezone; Sunday bug fixed |
| `Appointment.patient` (name) | `lead_id` (render name via lead) | joined read or name lookup |
| `Appointment.treatment` | `service_id` | |
| `Appointment.doctor` | `practitioner_id` | |
| `Appointment.branch` | `location_id` | |
| `Appointment.status Confirmada` | `state: confirmed` | "Por confirmar/No respondió" → FUTURE followup; client-side badge today |

### ERROR MAPPING
| Backend | UI |
|---|---|
| 404 NOT_FOUND | toast "no encontrado"; detail modal closed |
| 409 SLOT_BLOCKED / APPOINTMENT_CONFLICT (23P01) | toast "horario no disponible" |
| 409 CAPABILITY_MISSING / ENTITY_INACTIVE | toast with message; doctor selector re-validated |
| 403 PERMISSION_DENIED | toast "sin permisos" |
| 422 INVALID_INPUT / 409 IDEMPOTENCY_KEY_REUSED | toast; form kept |
| 5xx / network | generic error state, retry button |

### AUTH/CONTEXT
- Today: PF3 default context (bootstrap org, system principal) — everything passes; `X-Correlation-Id` forwarded. The adapter must send no auth headers yet; when identity lands, the same seam carries it (frontend must not branch on identity).
- New read endpoints enforce `appointments.read`/`leads.read` at service level from day one (deny-by-default, PF2 reusable).

### IDEMPOTENCY
- `POST /appointments` sends `Idempotency-Key` (uuid4 per user intent, stable across retries); replay → 201 same body + `Idempotent-Replay: true` → frontend shows success, never duplicates. Same for reschedule/cancel when wired.

### LOADING/ERROR STATES
- Agenda: skeleton on load; empty state "sin citas en la semana"; error banner + retry; filter changes re-query.
- Nueva cita: submitting state (disabled button), success toast, form reset; failure keeps form + shows mapped error.

### TESTS
- Backend (OdontoFlow): `GET /appointments` list/filter/tenant-scope/idempotency-not-needed tests; `GET /appointments/{id}` NOT_FOUND cross-tenant; `GET /leads`, `GET /locations` list tests; existing 294 stay green.
- Frontend: adapter unit tests (day/time derivation incl. Sunday; error mapping; idempotency header presence + replay handling); component tests for Agenda (mock axios via OpenAPI-shaped fixtures) and Nueva cita modal; one integration test with `VITE_USE_MOCKS=false` against a live backend (CI).

### FILES TO CHANGE
- Backend: `app/scheduling/router.py`, `app/scheduling/query.py` (or service) — read endpoints; `app/commercial/router.py` + `service.py` — lead list; `app/organization/router.py` — location list; `app/iam/permissions.py` unchanged (codes exist); tests: `tests/test_integration_reads.py` (new).
- Frontend: `src/api.ts`, `src/pages/AgendaPage.tsx`, `src/components/AppShell.tsx`, `src/types.ts` (or new `src/contracts/`), new adapter + tests.
- Contract artifacts: regenerate `docs/api/openapi.*`; frontend types generated from it.

### ACCEPTANCE CRITERIA
1. Agenda renders a real week from `GET /appointments` (org-scoped; a cross-tenant id returns 404).
2. "Nueva cita" creates a confirmed appointment via the real domain (capability + availability validated by backend); double-submit with the same Idempotency-Key creates exactly one appointment (PF4 proof visible in tests).
3. Slot conflict returns the mapped Spanish error, agenda unchanged.
4. The Pacientes/Caja/Inventario/Chat screens keep working in mock mode (VITE_USE_MOCKS=true) untouched.
5. No Patient/Finance/Inventory/Chat backend code is added; simulator code is untouched; full backend suite (294) green; frontend vitest suite green.
6. PF1–PF4 invariants covered by new endpoint tests.
