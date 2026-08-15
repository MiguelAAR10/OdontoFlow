# Documentation index

This directory has two kinds of content, and it matters which one you're reading.

**Curated docs (this layer)** are living documents: they describe the *current* product and architecture and
get rewritten as the system changes. Read these first.

**`superpowers/` (specs, plans, evidence, handoffs)** is an append-only engineering record: each file
describes what was true, decided, or proven *at the time it was written*, and is never rewritten afterward.
Read these when you need the authority behind a decision, or the exact evidence a milestone shipped with.

## Curated docs — start here

| Document | Answers |
|---|---|
| [`product-vision.md`](product-vision.md) | What is OdontoFlow, and why does it exist? Where is it going long-term (clearly marked FUTURE)? |
| [`architecture.md`](architecture.md) | How does the system implemented at `HEAD` actually work, in five minutes? What does PostgreSQL enforce? What is *not* true yet, even inside "closed" milestones? |
| [`backend-platform-blueprint.md`](backend-platform-blueprint.md) | The detailed technical authority: every architectural principle and *why* it exists, the full Lead→Appointment lifecycle, why PF1→PF2→PF3→PF4 has to happen in that order, and the MediStock domain-migration map (with an evidence-based inventory finding). |
| [`backend-evolution.md`](backend-evolution.md) | How did the backend get here? Commit-by-commit, with the actual test counts each milestone shipped with. |
| [`roadmap.md`](roadmap.md) | DONE / NOW / NEXT / LATER, at an architectural level. |

The root [`README.md`](../README.md) is the public entry point — a five-minute summary that links into the
four documents above rather than repeating them.

## `docs/superpowers/` — the engineering record

| Subdirectory | Contents | Authoritative for |
|---|---|---|
| `specs/` | Approved design documents, written **before** implementation | *What was authorized to be built*, and the exact contracts (schema, invariants, error codes) implementation must match. `2026-08-14-platform-foundation-design.md` (PF0) is the standing authority for PF1–PF4; `2026-08-12-lead-to-appointment-design.md` is the standing authority for Vertical 1. |
| `plans/` | Task-breakdown plans for a design | How a spec was decomposed into sequenced tasks. |
| `evidence/` | Read-only audits of the codebase as it existed at a point in time | Facts a design decision was based on — cite file:line, never opinion. |
| `handoffs/` | One report per completed task/block, written by whoever implemented it | *What actually shipped*: files changed, tests added, deviations from the brief (and why), blockers, and the recommended next step. Each PF1/PF2/PF3 handoff is the ground truth for what that block delivered — `architecture.md` and `backend-evolution.md` summarize them but the handoffs are authoritative on detail. |

`docs/api/` holds a generated OpenAPI snapshot (`openapi.yaml` / `openapi.json`) — the exact HTTP contract as
of the commit that generated it, not hand-maintained. **At the time of writing, `docs/api/` and `AGENTS.md`
exist in the working tree but are not committed to git** — a hygiene gap, not a content gap; see the handoff
below.

## A note on one stale document

`docs/superpowers/specs/2026-08-14-backend-documentation-design.md` proposed a different documentation layout
(`CLAUDE.md`, `CHANGELOG.md`, `docs/architecture/backend.md`, `docs/quality-and-testing.md`,
`docs/MIGRATION.md`) than the one you're reading. Only the root README and the `docs/api/` snapshot from that
proposal were ever delivered; the other four files do not exist in this repository. It is left in `specs/` as
part of the historical record — per the rule above — rather than edited or deleted. See
`docs/superpowers/handoffs/2026-08-14-github-repository-consolidation-handoff.md` for the full note and
`backend-evolution.md`'s closing section for the short version.

## One fact, one home

A fact about the system should have exactly one authoritative document. When curated docs and
`superpowers/` disagree, prefer curated docs for *current state* and `superpowers/` for *why a past decision
was made*. If you find an actual contradiction (not just a difference in altitude), that's a documentation
bug — check the most recent handoff and commit before trusting either side.
