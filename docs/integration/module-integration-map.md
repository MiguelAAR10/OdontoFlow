# Module Integration Map

Frontend `ODONTO-SMART-FRONT` (HEAD `8769f12`) ↔ Backend `OdontoFlow` (HEAD `34cfbf7`, PF1–PF4 CLOSED).
M = MATCH (direct reuse) · A = ADAPTER (semantic mapping) · G = BACKEND GAP (endpoint missing) · F = FUTURE (no backend authority; stays mock) · R = REFERENCE (never copied).

## Page/module → backend module

| Frontend module | Frontend surface | Backend module | Class | Notes |
|---|---|---|---|---|
| `AgendaPage` | weekly grid, filters, detail modal | `app/scheduling` | A/G | read endpoints missing (G); grid rendering adapter (A) |
| `AppShell` "Nueva cita" | booking modal | `app/scheduling` + `app/commercial` (Lead) + `app/catalog` (Service) + `app/organization` (Location/Practitioner) | A/G | semantic mapping; needs `GET /leads`, `GET /locations` (G) |
| `PatientsPage` | list/search/create "patient" | `app/commercial` (Lead) | A | maps to **Leads** (CRM); Patient domain is F |
| `Header` search | global patient search | `app/commercial` (Lead) | A/G | lead list + search (G) |
| `AgentPage` | KPIs, feed, queue, config, automations | — | F | agent runtime not designed; prototype |
| `CashPage` | movements, close-cash | — | F | Finance deferred |
| `InventoryPage` | products, purchases, stock | — | F | Inventory deferred |
| `ChatPage` | conversations, messages, create-appointment | — | F | chat/agent runtime not designed; voice = future structured-draft workflow |
| `src/api.ts` | client calls | real API | A | adapter layer to be introduced; mock switch preserved |
| `src/types.ts` | UI types | OpenAPI schemas | A | replace per-vertical with generated contracts; keep UI-only types (tone, badge maps) separate |
| `src/mockData.ts` | mock payloads | — | F/R | design-time + prototype only; never authoritative shapes |
| `src/domain`, `src/simulation`, `src/server.ts` | simulator harness (FollowupEngine, ReminderScheduler, clock, sim repo) | — | **R** | reference only; **never copied into OdontoFlow**; followup cadence = spec input for a future module |
| `db/` (frontend) | simulation PostgreSQL | — | R | sim persistence only; unrelated to OdontoFlow schema |
| `test/*` (vitest) | simulator + flow tests | — | R | reference for future followup module tests; SPA component tests to be added |

## Backend module → frontend consumption

| Backend module | Exposed today | Frontend consumer | Status |
|---|---|---|---|
| `catalog` (Service) | `GET/POST /services` | Nueva cita treatment selector | M (existing) |
| `commercial` (Lead) | `POST /leads`, `GET /leads/{id}` | Pacientes screen, booking patient selector | G (list missing) |
| `organization` (Location/Practitioner/Capability) | `POST /locations`, `POST /practitioners`, `POST /capabilities`, `GET /practitioners/eligible` | branch/doctor selectors | M (eligible) + G (location list) |
| `scheduling` | rules/blocks/slots/book/cancel/reschedule (idempotent) | Agenda + Nueva cita + Editar cita | G (read endpoints) |
| `iam` (permissions) | service-level only | — | F (no HTTP surface needed by vertical 1) |
| `audit` | service-level only | — | F |
| `idempotency` (PF4) | book/cancel/reschedule | Nueva cita double-submit protection | M (must be used by frontend) |
| Patient/Visit/ServiceExecution/Finance/Inventory/Chat/Followup | absent (deferred) | Pacientes/Caja/Inventario/Chat screens | F (mock until authority exists) |

## Simulator reuse decision

- The frontend's FollowupEngine/scheduler/simulation behavior (cadence rules, status gates, Lima-time math, idempotent sim adapters) is treated as **reference only** — input for a future Followup domain module spec, never vendored code.
- The OdontoFlow backend keeps zero Node/simulator artifacts; its own deterministic scheduling authority (GiST, slots, PF4) remains the only runtime truth.
