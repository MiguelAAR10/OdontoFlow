# Data Flow

Integration data flows between ODONTO-SMART-FRONT (HEAD `8769f12`) and OdontoFlow (HEAD `34cfbf7`).
Authority: FastAPI/PostgreSQL. Mock mode (`VITE_USE_MOCKS=true`) remains a design-time fallback; the flows below describe the real integration path.

## 1. Agenda read flow (first vertical)

```
AgendaPage → api.getAppointments(adapter)
  → GET /appointments?from&to&location_id&practitioner_id      [backend: NEW read endpoint, org-scoped]
  → OpenAPI AppointmentRead[] {id, lead_id, service_id, practitioner_id, location_id, start_utc, end_utc, state}
  → adapter: start_utc → day (0-5) + time via Location.timezone (IANA); patient name via lead join
  → AgendaPage grid (React visual design unchanged)
Detail modal → GET /appointments/{id}  [backend: NEW]  → 404 NOT_FOUND cross-tenant
```

Tenant: `organization_id` from the PF3 context (today: bootstrap org, system principal). Reads permission-checked (`appointments.read`) at service level. No idempotency on reads.

## 2. Booking flow

```
"Nueva cita" modal
  → selectors: GET /leads (NEW), GET /locations (NEW), GET /services (exists), GET /practitioners/eligible (exists)
  → POST /appointments {lead_id, service_id, location_id, practitioner_id, start}
       headers: Idempotency-Key: <uuid4 per user intent>, X-Correlation-Id
  → 201 AppointmentRead | 409 SLOT_BLOCKED/APPOINTMENT_CONFLICT (23P01)/CAPABILITY_MISSING | 403 | 422
  → on replay (Idempotent-Replay: true): same 201 body → frontend success, no duplicate
  → window event "appointment-created" → agenda refresh
```

PF4 claim-first ordering runs inside the booking transaction; double-submit with the same key yields exactly one appointment + one audit row (proven by `tests/test_idempotency.py`).

## 3. Reschedule/cancel flow (future wiring)

```
Editar cita → POST /appointments/{id}/reschedule {new_start} + Idempotency-Key  (exists)
Cancelar cita → POST /appointments/{id}/cancel + Idempotency-Key                 (exists)
```
Same error/idempotency mapping as booking.

## 4. Mock-only flows (no backend authority — prototype)

- Pacientes/Caja/Inventario/Chat screens, Agent KPIs/queue/config, close-cash: data stays in `mockData.ts`/component state. No network calls in mock mode; in `VITE_USE_MOCKS=false` these surfaces must render an explicit "prototype" state instead of hitting nonexistent endpoints.
- "Crear cita desde conversación": future structured-draft workflow (draft → user confirmation → booking command); never a direct DB writer.

## 5. Simulator flow (isolated harness, reference only)

```
SimulationClock (virtual Lima time) → ReminderScheduler → FollowupEngine gates
  → SimulatedWhatsApp/CallService (idempotent by key) → PostgresSimulationRepository
  → HTTP panel (127.0.0.1:3000)  — no relation to the SPA client paths
```
PostgreSQL "simulation_sessions" schema is independent of OdontoFlow's schema. No OdontoFlow code consumes it; no frontend sim code enters OdontoFlow.

## 6. Identity/tenant flow

```
Browser → FastAPI route → resolve_http_context(request)
  → ExecutionContext {organization_id: bootstrap=1, principal_id: system, principal_type: system,
                      request_id: uuid4, correlation_id: X-Correlation-Id | request_id}
  → service require_permission(code, location_id) → PF3 audit provenance → PF4 claim/replay
```
No auth headers yet (PF3 ≠ login). The frontend must not branch on identity; when real identity lands, only the resolution seam changes.

## 7. Audit/observability flow

Every mutation writes an `audit_events` row atomically with the mutation (PF3). The frontend forwards `X-Correlation-Id` so support can trace UI action → audit row. Idempotent replays produce no extra audit rows.

## 8. Error flow

```
FastAPI AppError → JSON envelope {error:{code,message,details}} → frontend error mapper
  → toast / inline state per screen (see contract doc ERROR MAPPING)
23P01 → 409 APPOINTMENT_CONFLICT (transport-mapped) · 40P01 → one-shot retry → 409 if repeated
Never: SQLSTATE, constraint names, stack traces (asserted by backend tests)
```
