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

## NOW

- **Agenda ↔ Scheduling vertical** (frontend) — shipped read endpoints consumed by the real frontend adapter
  (booking/reschedule/cancel with `Idempotency-Key`); remaining screens (Pacientes→Leads, Caja, Inventario,
  Chat, Agente) stay MOCK/PROTOTYPE until their domain authority exists.

## NEXT

- **Platform Foundation closure.** Close the gaps recorded in `architecture.md` §9 before building further:
  wire `provision_system_access` into `create_organization`; extend `ExecutionContext`/permission enforcement
  beyond the scheduling endpoints; resolve BLOCKER-2 (whether a location-scoped principal can create a
  `Lead`, given `Lead` has no `location_id` today).
- **Clinical Bridge.** `Appointment → Patient → Visit → ServiceExecution` — proposal synthesized from legacy
  evidence in `docs/superpowers/handoffs/2026-08-15-accelerated-core-sprint-handoff.md`; the first entity a
  future Finance or Inventory vertical can safely reference.

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
