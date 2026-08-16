# Backend Evolution

How OdontoFlow reached its current state, grounded in commits, migrations, and the handoffs each commit
shipped with. All SHAs below are verified against `git log` on `main` at the time of writing.

## Milestone table

| Milestone | Commit | What changed | Test count | Status |
|---|---|---|---|---|
| Task 1 — seed repo | `3504b66` | FastAPI `/health`, pytest, Docker Compose Postgres | — | CLOSED |
| Task 2 — persistence foundation | `58c3655` | config, db session, models, migration `0001` with the GiST booking invariant | — | CLOSED |
| Task 3 — error contract | `084a8d5` | stable `{"error": {code, message, details}}` envelope | — | CLOSED |
| Task 4 — catalog & organization | `6069ab5` | operational catalog, practitioner eligibility | — | CLOSED |
| Task 5 — commercial lead | `efc87a8` | `Lead` application slice | — | CLOSED |
| Task 6 — availability engine | `92bceed` | deterministic, pure slot engine | — | CLOSED |
| Task 7 — booking | `952a19b` | transactional appointment booking (GiST-backed) | — | CLOSED |
| Task 8 — FastAPI API | `f812c04` | lead-to-appointment HTTP surface exposed | — | CLOSED |
| Task 9 — cancel/reschedule | `e1d956c` | appointment cancellation and rescheduling | 172 PASS | CLOSED |
| Task 10 — E2E closure | `4086dc1` | **Vertical 1 (Lead → Appointment) CLOSED** — full HTTP journey proof | 174 PASS | CLOSED |
| Platform readiness evidence | `e5f3ba6` | two read-only audits (tenancy, actors/API/idempotency/trace) — no code change | 174 PASS | CLOSED |
| PF0 — platform foundation design | `0601b09` | design spec only; freezes tenancy, IAM, context, and idempotency contracts for PF1–PF4 | 174 PASS | CLOSED (design) |
| PF1 — org & tenant integrity | `4ff2de5` | migration `0002`; composite tenant FKs; `Organization`/`PractitionerMembership` | 217 PASS | CLOSED |
| PF2 — principal & authorization | `44ba874` | migration `0003`; `app/iam/`; permission-based IAM, deny-by-default | 258 PASS | CLOSED |
| PF3 — ExecutionContext & audit provenance | `1a737b0` | explicit context wired into booking/cancel/reschedule; audit provenance | 274 PASS | CLOSED, scoped (see `architecture.md` §9) |
| Documentation release (README + API snapshot) | `82d477e` | comprehensive root README, `docs/api/openapi.{yaml,json}` | 274 PASS | CLOSED |
| Documentation architecture design | `f7b2b0d` | `docs/superpowers/specs/2026-08-14-backend-documentation-design.md` — proposed a documentation IA (see note below) | 274 PASS | PARTIALLY DELIVERED (see note) |
| Execution-context coverage clarification | `11d2ad6` | README wording fix | 274 PASS | CLOSED |
| **PF4 — idempotent commands** | *(none — not started)* | designed in PF0 §21 (`command_receipts`, `CommandReceipt`); no migration `0004`, no code in `app/` at `HEAD` | — | **PENDING — verified absent** (`grep -r CommandReceipt app/` returns nothing) |

Test counts above are as recorded in each milestone's own handoff (`docs/superpowers/handoffs/`), verified
against real PostgreSQL, not re-run for this document. `pytest --collect-only` at `HEAD` currently collects
**274 tests**, consistent with the PF3 handoff's final count.

## The path, narrated

1. **Lead-to-Appointment vertical (Tasks 1–10).** The smallest real operational loop: register a `Lead`,
   maintain a `Service` catalog and `Practitioner` capabilities, publish availability, query deterministic
   slots, and book/cancel/reschedule a confirmed `Appointment` — all the way to a full HTTP end-to-end proof.
   Single tenant, no identity, no authorization: intentionally out of scope for this vertical
   (`docs/superpowers/specs/2026-08-12-lead-to-appointment-design.md`).
2. **Tenant-readiness analysis.** Before building more on top of Vertical 1, two read-only evidence audits
   examined the actual ownership/FK graph and the actor/API/idempotency/trace surface
   (`docs/superpowers/evidence/platform-readiness-*.md`), consolidated into a gate report
   (`docs/superpowers/handoffs/2026-08-13-platform-readiness-evidence.md`). No design decision was made in
   this step — only verified facts about what existed.
3. **PF0 — Platform Foundation design.** A pure specification (`docs/superpowers/specs/2026-08-14-platform-foundation-design.md`)
   that froze four contracts for PF1–PF4 to implement: tenancy (composite FKs, `Organization` as tenant
   root), identity & authorization (`Principal`, permission-based IAM), execution provenance
   (`ExecutionContext`), and command identity (`CommandReceipt`, not yet built). It also named two blocking
   product questions (identity resolution before authentication exists; whether a location-scoped principal
   can create a `Lead`) — the second one is still open (see `roadmap.md`).
4. **PF1 — tenant integrity migration.** Made `Organization` the tenant root and `Location` a branch inside
   it. Every tenant-owned table gained a direct `organization_id` and the composite-FK pattern that makes a
   cross-tenant relational state a PostgreSQL rejection, not an application check. `Practitioner` stayed
   global, reachable per organization only through `PractitionerMembership`. The practitioner-global GiST
   exclusion was verified byte-for-byte unchanged.
5. **PF2 — IAM foundation.** Added `Principal`, `Membership`, `Permission`, `Role`, `RolePermission`,
   `RoleAssignment` (migration `0003`), and a live, deny-by-default permission evaluation with a concrete
   nullable `location_id` scope. No transport wiring yet — proven at the service layer only.
6. **PF3 — provenance.** Made `ExecutionContext` an explicit, mandatory parameter for appointment
   booking/cancellation/rescheduling; wired live permission checks into those same three HTTP endpoints; made
   `AuditEvent` provenance derive from the resolved context (organization, principal, principal type,
   request id, correlation id) instead of always defaulting to `"system"`/`NULL`.
7. **PF4 — pending/current.** Idempotent commands (`CommandReceipt`, exactly-once semantics for booking,
   cancellation, and rescheduling) are designed in PF0 §21 but **not implemented** — no `command_receipts`
   table, no migration `0004` exists in this repository. This is the next unit of work; see
   [`roadmap.md`](roadmap.md).

## MediStock's role

Where this repository's own sources describe MediStock, they are consistent on one point: MediStock (the
legacy Flask backend) is a **read-only behavioral reference**, kept untouched (`../../medistock` in this
repository's layout), never a codebase this project edits. `AGENTS.md` states it explicitly: OdontoFlow "is
not a 'MediStock rewrite in FastAPI'" — it is a fresh architecture (tenant model, permission model, execution
context, transaction ownership) designed against PostgreSQL from PF0 onward, that happens to reuse proven
*domain knowledge* (what a dental clinic's lead-to-appointment flow needs to do) from the legacy system. No
MediStock code, schema, or endpoint was migrated table-for-table; this repository does not inspect or modify
`../../medistock`.

## A stale specification, for the record

Commit `f7b2b0d` (`docs: define backend documentation architecture`) proposed a documentation set — `CLAUDE.md`,
`CHANGELOG.md`, `docs/architecture/backend.md`, `docs/quality-and-testing.md`, `docs/MIGRATION.md` — of which
only the root `README.md` and the `docs/api/` OpenAPI snapshot were actually delivered. The other four files
were never created. `AGENTS.md` still references `docs/MIGRATION.md` as if it exists. This document, together
with [`architecture.md`](architecture.md) and [`docs/README.md`](README.md), supersedes that proposal's intent
without reusing its exact file layout — see the handoff at
`docs/superpowers/handoffs/2026-08-14-github-repository-consolidation-handoff.md` for the full note.
