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

## Economic & Operations Bridge — PF6 (2026-08-15)

- Added `app/economics/` (migration 0006): `Product` (org-owned catalog,
  declared kind consumible/reventa, no stock authority), `ServiceConsumption`
  (execution-anchored, `UNIQUE(org, execution, product)`, quantity/price
  snapshot), `Charge` (1:1 per execution, amount from the execution price
  snapshot), `Payment` (N:1, derived paid/outstanding, deterministic
  overpayment rejection via charge row lock).
- Eight new permission codes seeded and granted to every `system` role.
- PF4 claim-first idempotency on all four creates; PF3 audit atomic.
- PF gap fix: `create_organization` provisions system access atomically
  (PR7) — runtime organizations are immediately operable.
- Frontend (separate repo): Patients screen integrated with the real clinical
  API (list/create via OpenAPI types, loading/error states).

## Inventory Ledger + PF Closure — PF7 (2026-08-16)

- Added `app/inventory/` (migration 0007): append-only `inventory_movements`
  ledger (ENTRADA/SALIDA/ADJUSTMENT with per-type CHECKs; reason-required
  adjustments) and the derived read-time `InventoryBalance` — no stock column,
  no trigger cache, one authoritative mutation path.
- Consumption now emits its SALIDA movement in the same transaction (1:1 via
  `id_consumo_origen UNIQUE` + a DB trigger enforcing product causality), with
  the negative-balance guard (product row lock + ledger sum) proven under
  concurrency.
- New endpoints: `POST /products/{id}/entries`, `POST /products/{id}/adjustments`,
  `GET /products/{id}/movements`, `GET /products/{id}/balance` (PF4-idempotent
  creates, `movements.read/create` permissions).
- PF closure: every remaining mutating service (lead, service, location,
  practitioner, membership, capability, availability rule/block) is now
  ctx-gated permission-checked; BLOCKER-2 resolved (lead creation is
  org-wide-only, E5).
