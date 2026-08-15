# Product Vision

## What OdontoFlow is

OdontoFlow is a **multi-tenant operational ERP** for dental clinics: a deterministic system of record where
humans, agents, integrations, and system processes all operate against the **same** business state, through
the **same** typed contracts, under the **same** authorization and audit rules. There is no privileged bypass
path for any kind of actor.

## Why it exists

Clinic operations software is usually one of two things: a rigid legacy system that no one can safely extend,
or an LLM-wrapped assistant with no durable, constraint-enforced state underneath it. OdontoFlow is built to
be neither. It starts from the smallest real operational loop — a commercial lead becomes a confirmed,
conflict-free appointment — and grows outward, keeping PostgreSQL as the non-negotiable source of truth at
every step, so that adding an AI-driven caller later never means loosening what the database is allowed to
guarantee.

OdontoFlow's own engineering record documents it as the *successor* of a legacy Flask/MediStock backend, not
a like-for-like rewrite of it. See [`backend-evolution.md`](backend-evolution.md) for what that means in
practice and what MediStock's role actually was.

## Long-term direction (FUTURE — not implemented)

```
CRM (Lead)
  → Scheduling (Appointment)                         ◄── IMPLEMENTED (Vertical 1, closed)
    → Clinical (Patient, Visit, ServiceExecution)     ◄── FUTURE
      → Finance (Charge, Payment)                     ◄── FUTURE
        → Inventory / Operations (Product, Stock, Consumption)   ◄── FUTURE
          → Optimization / agent execution            ◄── FUTURE
```

Everything below "Scheduling" in this chain is **planned direction, not built capability**. See
[`roadmap.md`](roadmap.md) for what is actually DONE / NOW / NEXT / LATER as of this document.

The intended shape of each future stage, in one sentence each:

- **Clinical Bridge** — a confirmed appointment produces a `Visit`; a `Visit` against a real `Patient` (not a
  pre-clinical `Lead`) produces a `ServiceExecution` — the first entity clinical and financial work can hang
  off safely.
- **Finance** — `Charge` and `Payment` attach to a `ServiceExecution`, never to a bare appointment or a
  guessed price.
- **Inventory / Operations** — stock is ledger-based (`Product`, immutable `StockMovement`, derived
  `StockBalance`), and clinical consumption is tied to a real `ServiceExecution` — never a direct mutable-stock
  decrement, which is the failure mode the legacy system had.
- **Optimization / agent execution** — agents reason over state, constraints, and candidate actions, then
  call the same deterministic tools a human would use to commit anything. The optimization/world-model layer
  itself is explicitly **not designed yet** anywhere in this repository.

## The non-negotiable principle

> LLMs may interpret, plan, and propose actions, but deterministic domain rules, authorization, optimization
> constraints, and PostgreSQL decide what is valid and what is committed.

Concretely, this means, today and for every future vertical:

- Service duration, availability, practitioner capability, appointment conflicts, tenant boundaries, and
  authorization are enforced in application code and PostgreSQL constraints — never inferred from a model
  response.
- An agent is a `Principal` like any other (`type = 'agent'`), authorized through the exact same
  permission/membership/role evaluation a human staff member goes through. There is no separate, looser code
  path for automated callers.
- External systems (a future calendar sync, WhatsApp, billing/NubeFact) are **adapters** — they may
  synchronize with or request actions from OdontoFlow, but they never become a domain authority.
- No component under `app/` imports an LLM SDK or agent framework today; see
  [`architecture.md`](architecture.md) §8. Any future agent integration is expected to be a caller *of* this
  API surface, not a replacement for the rules inside it.

## Target ERP module map (FUTURE — not implemented)

The future platform organizes around seven bounded contexts. Only **Commercial/CRM** and **Scheduling** are
implemented today; everything else below is planned direction — see
[`backend-platform-blueprint.md`](backend-platform-blueprint.md) §9–§10 for what is actually built and what
was learned from MediStock about how *not* to build the ones that aren't.

| Bounded context | Owns | Status |
|---|---|---|
| **Commercial / CRM** | `Lead`, acquisition source | IMPLEMENTED |
| **Scheduling** | `Service` (canonical catalog), `Location`, `Practitioner`, capability, availability, `Appointment` | IMPLEMENTED |
| **Clinical** | `Patient`, `Visit`, `ServiceExecution` | FUTURE (Clinical Bridge — NEXT on the roadmap) |
| **Finance** | `Charge`, `Payment`, `Invoice`, pricing adjustments | FUTURE |
| **Inventory / Operations** | `Product`, `ServiceConsumption`, `InventoryMovement`, `InventoryBalance`, transfers | FUTURE |
| **Integrations** | Calendar, WhatsApp, Email, billing, voice/STT adapters | FUTURE |
| **Intelligence / Optimization** | Agents reasoning over deterministic state via tool calls | FUTURE, and deliberately undesigned beyond principle (see below) |

### Target domain spine

```
Lead → Appointment → Patient → Visit → ServiceExecution
                                              │
                        ┌─────────────────────┴─────────────────────┐
                        ▼                                           ▼
                 Charge → Payment → Invoice           ServiceConsumption → InventoryMovement → InventoryBalance / Transfer
```

**The rule that must hold across every context above:** the *same* canonical `Service` row (already
implemented in `app/catalog`) is what Scheduling, Clinical, Finance, and Inventory/Operations all reference —
no vertical introduces its own service catalog. MediStock's own history has a concrete example of what
happens when that discipline slips (two separate model classes mapping to the same table); see
`backend-platform-blueprint.md` §10.

A `Visit` is anchored to a real `Patient` (organization-owned directly, per PF0's P10), not to the
pre-clinical `Lead` — a `Lead` and a `Patient` are deliberately different entities with no automatic
promotion path assumed yet. A `ServiceExecution` is what a `Charge` or a `ServiceConsumption` attaches to;
neither ever attaches to a bare `Appointment` or a guessed price, because an appointment can be rescheduled or
cancelled and a `Lead` was never a clinical relationship to begin with.

## Operational intelligence vision (FUTURE — not implemented, not designed beyond this principle)

Eventually, OdontoFlow should let agents reason over a **deterministic, reliable picture of world state** —
capacity, schedules, locations, treatment plans, prices, costs, margins, inventory, payments, demand,
conversion, campaigns — to go from *state* → *candidate action* → *outcome*, using business metrics,
optimization models, constraints, policies, forecasting, and tool calls.

**What this explicitly is not, today:**

- There is no trained world model. Nothing in this repository learns a policy or a forecast from data yet.
- No fine-tuning, RAG, or LoRA approach is prescribed. Choosing one now, before the underlying data exists,
  would be designing the roof before the foundation.
- The precondition this vision depends on is **reliable state/action/outcome data** — which itself depends on
  the platform foundation already built: an `ExecutionContext` on every action, an `AuditEvent` on every
  outcome, and (once PF4 ships) an idempotent command boundary so a retried action isn't miscounted as two.
  Optimization and forecasting are only as trustworthy as the data trail underneath them, and that trail is
  exactly what PF1–PF4 exist to guarantee.

When this vision starts becoming concrete work, it earns its own spec under `docs/superpowers/specs/` like
every other platform block — it is named here only so the destination is legible, not so it can be started
early.

## Integration architecture (FUTURE — not implemented)

Planned adapters: **Google Calendar**, **WhatsApp**, **Email**, **billing/invoicing** (e.g. NubeFact), and
**voice/STT**. The invariant that governs all of them, already true of the platform's design even though none
of these adapters exist yet:

> OdontoFlow remains the operational source of truth. External systems synchronize *with* OdontoFlow; they
> never become a business authority.

Concretely: a Calendar sync reflects OdontoFlow's `Appointment` state outward (and may report external
conflicts inward as information), but a slot is never considered booked because an external calendar says so
— PostgreSQL's GiST exclusion is still the only thing that commits a booking. A billing adapter renders an
`Invoice` OdontoFlow already computed; it does not compute pricing itself. This mirrors the same principle
already governing agents (§"The non-negotiable principle" above): an adapter is a caller or a mirror, never a
decision-maker.

## Terminology used consistently across this documentation

| Term | Meaning |
|---|---|
| **Organization** | the tenant — one company / practice / clinic group; the security boundary |
| **Location** | a branch — a physical/operational scope that belongs to exactly one Organization |
| **Principal** | any actor that can issue a command: `human \| agent \| integration \| system` |
