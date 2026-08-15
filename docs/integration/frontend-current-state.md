# Frontend Current State — ODONTO-SMART-FRONT

HEAD: `8769f12` ("Implement ODONTO SMART frontend") · inspected read-only
Evidence: `.audit/frontend-integration/scout-a-report.md` (68 read-only tool calls) + lead spot-checks.

## Stack

- React 18 + Vite + Tailwind, React Router SPA (src/App.tsx:12-22), TypeScript.
- Axios client `baseURL = VITE_BACKEND_URL` (src/api.ts:22-24).
- `VITE_USE_MOCKS` defaults to `true` — **every screen renders mockData unless explicitly disabled** (.env.example:4; src/api.ts:26).
- A separate Node **simulation harness** (src/server.ts, port 127.0.0.1:3000) with its own PostgreSQL (`db/` migrations + seeds) — unrelated path surface to the SPA client.
- Tests: Vitest (`test/simulation.test.ts`, `reminder-flow.test.ts`, `end-to-end-followup.test.ts`, `ui.test.ts`) + a Playwright visual script.

## UI capabilities (screens and actions)

| Route | Screen | Real actions | Local-only / inert |
|---|---|---|---|
| `/agenda` | Weekly grid Mon–Sat, 09:00–14:00, hard-coded week "10–15 ago 2026", branch/status filters, appointment detail modal | filters, detail modal | "Editar cita" button inert (AgendaPage.tsx:84) |
| `/agente` | Agent KPIs (hard-coded 24/8/5/3), activity feed (Citas/Leads tabs), automations, human queue, config modal, assignment modal | tab switching, day filter | queue takeover + config toggles are local-only |
| `/pacientes` | Search name/DNI/phone, branch/status filters, table, detail modal, "Nuevo paciente" form → `createPatient` | search, create (mock) | pagination buttons inert |
| `/caja` | Movimientos/Comisiones/Links/Cierres tabs (only Movimientos real), KPIs hard-coded, income/expense forms | movements (mock) | close-cash local-only |
| `/inventario` | Productos/Compras/Consumo/Proveedores (only Productos real), filters, new product + register purchase | products (mock), purchase (provider field never sent) | — |
| `/chat` | Conversation list, composer → `sendMessage`, agent/human toggle (local), patient side panel, "Crear cita desde conversación" (hard-coded date/doctor/branch) | messages (mock) | toggle local; confirmation banner only for hard-coded `patientId === "ana"` |
| Global | Patient search in header (min 2 chars → `/pacientes?patient=id`) | search | notification/profile buttons inert |

## Data expectations (src/types.ts)

- `Patient {id, initials, name, dni, phone, branch, nextAppointment, treatment, status: Activo|Lead|Pendiente, tone, origin, interest}` (types.ts:3-16) — a **CRM-shaped** patient with branch + commercial fields.
- `Appointment {id, day: 0-5, time "HH:MM", patient (display name), treatment, doctor, branch, status: Confirmada|Por confirmar|No respondió}` (types.ts:18-27). `day` computed as `getDay()-1` clamped to [0,5] (api.ts:59) — Sundays collapse to day 0; no year/month.
- Fixed enums hard-coded in UI: branches "Lince/Jesús María/Magdalena", doctors "Dra. Valeria Ruiz/Dr. Mateo León" (AppShell.tsx:44-46); status→color tones (Badge.tsx:8-13).
- **Simulator domain types are incompatible** with the SPA types: `Appointment` with UUID FKs + `startsAt/endsAt: Date` + `appointmentStatus/followupStatus` enums (src/domain/types.ts:106-122). Same name, different shape — no adapter exists.

## API calls (src/api.ts — the SPA's declared client contract)

Only used when `VITE_USE_MOCKS !== "false"` is not true:

| Call | Path | Payload |
|---|---|---|
| `getPatients` | GET `/patients` | — |
| `createPatient` | POST `/patients` | `Omit<Patient,"id"|"initials"|"tone">` |
| `getAppointments` | GET `/appointments` | — |
| `createAppointment` | POST `/appointments` | `NewAppointmentInput {patient, treatment, doctor, branch, date, time}` |
| `getAgentDashboard` | GET `/agent/dashboard` | — |
| `getCashMovements` | GET `/cash/movements` | — |
| `createCashMovement` | POST `/cash/movements` | movement payload |
| `getProducts` | GET `/inventory/products` | — |
| `createProduct` | POST `/inventory/products` | product payload |
| `registerPurchase` | POST `/inventory/purchases` | `{productId, quantity}` |
| `getConversations` | GET `/conversations` | — |
| `sendMessage` | POST `/conversations/{id}/messages` | `{text}` |

None of these paths exist in the simulation harness either (which uses `/api/state`, `/api/clock`, `/api/scheduler/run`, `/api/demo/run`, `/api/appointments`, `/api/calls/...`) — pointing `VITE_BACKEND_URL` at the harness yields 404s.

## Mock-only behavior

- Default mode: all data from `src/mockData.ts`, deep-cloned per call; mutations mutate in-memory arrays (api.ts:29-33, 42-148).
- Pure-local (no mockData either): queue takeover, close-cash, agent config toggles, chat human/agent toggle (AgentPage.tsx:24-27; CashPage.tsx:90; ChatPage.tsx:65).
- Hard-coded KPIs on four pages; `updated: "14 ago 2026"` hard-coded on product creation (api.ts:112).

## Simulator behavior (reference only)

- `FollowupEngine` is the single source of truth for followup eligibility in the harness (docs/architecture.md:45): day-before 09:00 WhatsApp; day-before 12:00/16:00 calls; same-day 09:00; one-hour-before WhatsApp (FollowupEngine.ts:24-30); eligibility gates by appointment/followup status (33-45); Lima time UTC−5 (122-129).
- `ReminderScheduler.run` creates due messages/calls per appointment×rule (ReminderScheduler.ts:13-57).
- `SimulatedEventProcessor`: CONFIRM/CANCEL/REQUEST_RESCHEDULE/NO_RESPONSE state machine incl. reception-task on reschedule (54-91).
- Persistence: PostgresSimulationRepository with idempotent upserts (`ON CONFLICT (session, appointment, attempt_type)`, inbound-event dedupe, contact-attempt linkage) and a `simulation_sessions.simulated_now` virtual clock.
- The SPA never touches the simulator clock/state.

## Frontend domain assumptions (business rules the UI assumes)

1. Agenda is a fixed weekly Mon–Sat grid, 09:00–14:00, one branch/doctor per row cell.
2. Appointment confirmation cadence: confirm day before, call non-responders 12:00 and 16:00, same-day reminder 09:00, final re-confirm 1h before (mockData automations mirror FollowupEngine).
3. Appointment statuses only `Confirmada|Por confirmar|No respondió`; patients `Activo|Lead|Pendiente`.
4. Three fixed branches, two fixed doctors, WhatsApp as the only outbound channel; staff identity "Leonardo P.".
5. Inventory status derived `stock <= minimum`.
6. The SPA assumes no scheduling engine of its own — followups are static mock automations.

## Test coverage

- simulation.test.ts: clock + idempotent agenda/WhatsApp/call adapters.
- reminder-flow.test.ts: full cadence, reschedule task, scheduler idempotency, inbound dedupe.
- end-to-end-followup.test.ts: Ana demo journey + 10 scenarios.
- ui.test.ts: syntax-only check of server inline JS.
- **Not covered**: SPA components/pages, api.ts mock↔live switching, mockData mutation, HTTP routes, PostgresSimulationRepository, React rendering.

## Integration risks (top)

1. **Path mismatch (critical)**: SPA client paths vs simulator paths never match; `VITE_BACKEND_URL=http://localhost:8080` targets nothing.
2. **Shape mismatch (critical)**: two incompatible `Appointment` models (UI day/time/name vs domain startsAt/endsAt/UUID FKs); no adapter.
3. **No real backend integration surface exists** — the harness is a simulator, not an API.
4. Non-persistent UI state (queue, config, close-cash) and hard-coded KPIs/dates will contradict any real data source.
5. Date handling bugs: Sunday→day 0 collapse; local-time `T12:00:00` parse; ChatPage hard-codes 2026-08-17 outside the visible week.
