# Roadmap

Architectural roadmap, not a backlog. See [`backend-evolution.md`](backend-evolution.md) for the commit-level
history behind DONE, [`architecture.md`](architecture.md) §9 for gaps inside items marked DONE, and
[`backend-platform-blueprint.md`](backend-platform-blueprint.md) for why PF1→PF2→PF3→PF4 has to happen in
that order and what each NEXT/LATER item actually depends on.

## DONE

- **Lead → Appointment** (Vertical 1) — commercial lead, catalog, organization/practitioner capability,
  deterministic availability, transactional booking with GiST-enforced conflict safety, cancel/reschedule,
  full HTTP E2E proof. Closed at `4086dc1`.
- **Multi-tenant foundation** (PF1) — `Organization` as tenant root, composite tenant-consistency FKs,
  `Practitioner` as a global identity reachable per tenant via `PractitionerMembership`. Closed at `4ff2de5`.
- **Authorization** (PF2) — `Principal`/`Membership`/`Role`/`Permission` model, live deny-by-default
  permission evaluation, organization-wide and location-scoped grants. Closed at `44ba874`.
- **Provenance** (PF3) — explicit `ExecutionContext`, wired into booking/cancellation/rescheduling; audit
  rows carry organization, principal, principal type, request id, and correlation id. Closed at `1a737b0`.
  Scoped to those three endpoints only — see `architecture.md` §9.
- **Idempotent Commands** (PF4) — durable PostgreSQL `CommandReceipt`, exactly-once semantics for booking,
  cancellation, and rescheduling, no Redis, no middleware transaction ownership. Closed at `34cfbf7`.
- **Frontend integration reads** (Accelerated Core Sprint) — `GET /appointments` (date/location/practitioner
  filters, half-open window), `GET /appointments/{id}`, `GET /leads` (search), `GET /locations`; all
  tenant-scoped, permission-checked, OpenAPI-regenerated; agenda E2E proven against real FastAPI +
  PostgreSQL with the frontend's real adapter (no mocks). Closed at the Accelerated Core Sprint commit.
- **Clinical core** (PF5) — `Patient` (org-owned, per-org DNI), `Visit` (attended encounter, optional
  confirmed-appointment origin, walk-in mode), `ServiceExecution` (per-visit executed services with
  point-in-time price snapshot); PF2 permissions, PF3 audit, PF4 idempotency; 19 clinical tests; the
  economic/ops contract (`ServiceConsumption`/`Charge`/`Payment`) is designed, not implemented.
- **Economic & operations core** (PF6) — `Product` (org-owned, declared kind, no stock authority),
  `ServiceConsumption` (execution-anchored, price snapshot, one product per line), `Charge` (1:1 per
  execution, amount from the execution snapshot), `Payment` (N:1, derived paid/outstanding, deterministic
  overpayment rejection via row lock); 20 economic tests; runtime `create_organization` now provisions
  system access atomically (PR7 gap fix).

## NOW

- **Agenda ↔ Scheduling vertical** (frontend) — shipped read endpoints consumed by the real frontend adapter
  (booking/reschedule/cancel with `Idempotency-Key`); remaining screens (Pacientes→Leads, Caja, Inventario,
  Chat, Agente) stay MOCK/PROTOTYPE until their domain authority exists.

- **Inventory ledger** (PF7) — append-only `InventoryMovement` (ENTRADA/SALIDA/ADJUSTMENT with per-type
  CHECKs and reason-required corrections), derived read-time `InventoryBalance` (no stock column, no trigger
  cache), consumption→SALIDA 1:1 in the same transaction with a DB trigger for product causality, and the
  negative-balance guard via product row lock + ledger sum. Closed at the inventory commit.
- **PF closure** — every remaining mutating service (leads, services, locations, practitioners, capabilities,
  memberships, availability rules/blocks) is now ctx-gated permission-checked; BLOCKER-2 resolved (lead
  creation requires an org-wide grant); runtime `create_organization` provisions system access atomically.

## NEXT

- **Platform Foundation closure.** Remaining `architecture.md` §9 items (review any stragglers).
- **Sale stock-out** for `kind='reventa'` products (movement exists; invoice linkage deferred).
- **Finance follow-ups** (deferred): payment reversal, method catalog, Invoice/discount engine.
- **Multi-location stock & transfers** (additive later: `location_id` + TRANSFER type).

## LATER

- **Finance** — `Charge`, `Payment` attached to a `ServiceExecution`.
- **Inventory / Operations** — ledger-based stock (`Product`, immutable `StockMovement`, derived
  `StockBalance`), consumption tied to a real service execution — never a direct mutable-stock decrement.
- **External adapters** — Calendar sync, WhatsApp, billing/invoicing (e.g. NubeFact) as adapters around the
  domain, never as domain authorities.
- **Operational optimization / agent execution** — agents as `Principal`s calling the same deterministic
  tools a human uses; the optimization/world-model layer itself is explicitly not designed anywhere yet.

Everything under NEXT and LATER is **planned direction, not implemented capability** — see
[`product-vision.md`](product-vision.md).
