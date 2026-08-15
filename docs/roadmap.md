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

## NOW

- **PF4 — Idempotent Commands.** Durable PostgreSQL `CommandReceipt`, exactly-once semantics for booking,
  cancellation, and rescheduling, no Redis, no middleware transaction ownership. Designed in
  `docs/superpowers/specs/2026-08-14-platform-foundation-design.md` §21; not started in code.

## NEXT

- **Platform Foundation closure.** Close the gaps recorded in `architecture.md` §9 before building further:
  wire `provision_system_access` into `create_organization`; extend `ExecutionContext`/permission enforcement
  beyond the three scheduling endpoints; resolve BLOCKER-2 (whether a location-scoped principal can create a
  `Lead`, given `Lead` has no `location_id` today).
- **Clinical Bridge.** `Appointment → Patient → Visit → ServiceExecution` — the first entity a future
  Finance or Inventory vertical can safely reference, replacing the pre-clinical `Lead` once a real patient
  relationship exists.

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
