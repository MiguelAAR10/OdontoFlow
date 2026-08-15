# OdontoFlow Changelog

## PF4 — Idempotent Commands (2026-08-15)

- Added `command_receipts` table (migration `0004`): durable exactly-once
  execution for `appointments.book`, `appointments.reschedule` and
  `appointments.cancel`, keyed by `(organization_id, operation,
  idempotency_key)` with a canonical request fingerprint.
- Added `app/idempotency/` — the application-level command handler:
  claim-first ordering inside the existing service transactions, replay of
  the stored logical outcome on identical retries, deterministic
  `IDEMPOTENCY_KEY_REUSED` (409) on fingerprint/principal mismatch.
- Transport reads the optional `Idempotency-Key` header and signals replays
  with the non-authoritative `Idempotent-Replay: true` header.
- Agents and integrations must supply an idempotency key (`INVALID_INPUT`
  422); humans keep the previous contract; absent key writes no receipt.
- The practitioner-global GiST exclusion and the existing `23P01`/`40P01`
  behaviour are unchanged.

## Accelerated Core Sprint — Agenda Integration (2026-08-15)

- Added agenda read endpoints: `GET /appointments` (half-open date window,
  location/practitioner filters, joined display names), `GET /appointments/{id}`,
  `GET /leads` (search), `GET /locations` — all org-scoped and permission-checked
  (`appointments.read`, `leads.read`, `locations.read`), OpenAPI regenerated.
- Frontend (separate repo) wired the Agenda to these endpoints with
  OpenAPI-generated types and `Idempotency-Key` on booking/reschedule/cancel;
  E2E proven against real FastAPI + PostgreSQL with no mock data.
- Mechanical path fix: `../medistock` → `../../AI-EdgeRunners/medistock` in
  engineering docs after the workspace reorganisation.

## Clinical Core — PF5 (2026-08-15)

- Added `app/clinical/` (migration 0005): `Patient` (org-owned, per-org DNI
  partial unique), `Visit` (attended encounter; optional confirmed-appointment
  origin with derived practitioner/location, or walk-in), `ServiceExecution`
  (per-visit executed services, `UNIQUE(org, visit, service)`, point-in-time
  `executed_price` snapshot).
- Six new permission codes (`patients.*`, `visits.*`, `executions.*`) seeded
  and granted to every `system` role.
- All three clinical creates are PF4-idempotent; audit provenance atomic per
  mutation (PF3); composite FKs make cross-tenant states structurally
  impossible (PF1).
- Shared PF4 claim/settle helpers extracted into `app/idempotency/service.py`
  (scheduling refactored onto them).
