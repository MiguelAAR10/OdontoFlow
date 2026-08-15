# GitHub Repository Consolidation — Handoff

**Date:** 2026-08-14 · **Nature:** DOCUMENTATION ONLY. No `app/`, `alembic/`, `tests/`, or dependency file was
read for the purpose of changing it, and none was changed. Verified: `git diff --stat -- app/ tests/ alembic/
pyproject.toml` is empty.

## Objective

Make the repository understandable, credible, and maintainable for a technical reader within ~5 minutes,
without touching product code, by adding a curated documentation layer on top of the existing
`docs/superpowers/` engineering record.

## Documents created

| File | Purpose |
|---|---|
| `docs/product-vision.md` | Product definition, long-term direction (CRM → Scheduling → Clinical → Finance → Inventory → Optimization), the deterministic-authority principle, terminology table. Future stages explicitly labeled FUTURE. |
| `docs/architecture.md` | Current architecture only: module boundaries, PostgreSQL-enforced invariants (tenant composite FKs, practitioner-global GiST), transaction ownership, principal/permission model, ExecutionContext, deterministic API boundary, absence of LLM dependencies. One Mermaid diagram. §9 explicitly lists four verified gaps inside "closed" PF1–PF3 work (see below) so PF2/PF3 are not overclaimed as globally complete. |
| `docs/backend-evolution.md` | Commit-by-commit milestone table (SHA, result, test count) and a narrated explanation of how Vertical 1 → tenant-readiness analysis → PF0 → PF1 → PF2 → PF3 → PF4(pending) happened. Includes MediStock's role (read-only reference, not a rewrite target) and a note on the stale documentation-design spec. |
| `docs/roadmap.md` | DONE / NOW / NEXT / LATER only, architectural altitude, no backlog-level detail. |
| `docs/README.md` | Documentation index: curated docs vs. the append-only `superpowers/` record (specs/plans/evidence/handoffs), and the stale-spec note. |

## Documents changed

| File | Change |
|---|---|
| `README.md` | Rewritten for conciseness: kept product definition, "Why OdontoFlow," a compact architecture picture, current development status, tech stack, repository layout, running-locally (unchanged, already accurate), testing, and links. Removed ~230 lines of implementation detail (full API reference, error-code table, invariants/concurrency detail, domain-model ER table, audit/context detail) that now live in `docs/architecture.md`, so the root file summarizes and links instead of duplicating. Diff: 62 insertions / 229 deletions. |

No file under `docs/superpowers/` was moved, renamed, or edited — the historical record is untouched.

## Stale or misleading documentation found

1. **`docs/superpowers/specs/2026-08-14-backend-documentation-design.md` describes an undelivered scope.** It
   proposes `CLAUDE.md`, `CHANGELOG.md`, `docs/architecture/backend.md`, `docs/quality-and-testing.md`, and
   `docs/MIGRATION.md`. None of these five files exist in the repository. Only the root `README.md` rewrite
   and the `docs/api/` OpenAPI snapshot from that spec's scope were ever produced. `AGENTS.md` (§5, §8) still
   references `docs/MIGRATION.md` as if it exists. Left in place per the append-only rule for
   `docs/superpowers/`; noted here and in `docs/README.md`/`docs/backend-evolution.md` instead of edited.
2. **The pre-existing root `README.md` overstated PF2/PF3 completeness** by omission — it said "Platform
   Foundation (PF1–PF3) implemented" without qualification. The repository's own spec
   (`2026-08-14-backend-documentation-design.md`) already lists four concrete gaps that the previous README
   did not surface. `docs/architecture.md` §9 now states them explicitly:
   - HTTP resolves every request as the seeded `system` principal (no authentication exists).
   - `create_organization` does not call `provision_system_access` (new orgs lack system access until wired).
   - `ExecutionContext`/permission enforcement is wired to booking/cancellation/rescheduling only.
   - Appointment services keep a `ctx: ExecutionContext | None = None` compatibility path that skips the
     permission guard when `ctx` is omitted.

## GitHub hygiene findings

- **No secrets, `.env`, credentials, or `.audit/` contents are tracked in git.** `git ls-files` contains no
  `.env`, no `*secret*`/`*credential*` path, and no `.audit/*` path. `.gitignore` already excludes `.venv/`,
  `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.env`, `*.egg-info/`, `dist/`, `build/`, `.DS_Store`, and
  `.audit/`. **No `.gitignore` change was needed.**
- **`AGENTS.md` and `docs/api/` (`openapi.yaml`, `openapi.json`) are untracked.** `git status` shows both as
  `??`. Commit `82d477e` ("docs: add comprehensive project README with API reference") added only
  `README.md` and one `.gitignore` line — it never added `docs/api/`. `AGENTS.md` was never committed at all.
  This means a fresh clone of this repository from GitHub today would be **missing its own engineering
  contract file and its generated OpenAPI snapshot**, even though multiple documents (the prior README, the
  backend-documentation-design spec) refer to them as delivered. This is a real gap between working tree and
  what GitHub actually serves — flagged in `docs/architecture.md` §7 and `docs/README.md`, but **not fixed by
  staging or committing them**, since doing so was outside this task's scope (documentation content, not git
  operations) and committing is explicitly not requested here.
- `docs/superpowers/plans/.evidence/*.json` is tracked; this is historical engineering evidence, not a local
  artifact, so it was left in place per the "do not delete historical evidence" rule.

## Current product status (as documented)

Vertical 1 (Lead → Appointment) closed at `4086dc1`. Platform Foundation PF1 (`4ff2de5`), PF2 (`44ba874`), PF3
(`1a737b0`) closed, each with its own handoff. PF4 (Idempotent Commands) is designed in PF0 §21 but has no
code, migration, or table in this repository — it is the next unit of work. `pytest --collect-only` at `HEAD`
collects 274 tests, consistent with the PF3 handoff's final count; the full suite was not re-run against a
live PostgreSQL instance as part of this documentation task (no product code changed, so no regression risk
from this work).

## Documentation structure (final)

```
README.md                          — public entry point, concise, links out
docs/
  README.md                        — documentation index (curated vs. superpowers/)
  product-vision.md                — what/why, FUTURE direction
  architecture.md                  — current architecture + verified gaps (§9)
  backend-evolution.md             — commit-grounded milestone history
  roadmap.md                       — DONE / NOW / NEXT / LATER
  api/                              — generated OpenAPI snapshot (untracked — see hygiene note)
  superpowers/
    specs/                         — approved design authority (unchanged)
    plans/                         — task decomposition (unchanged)
    evidence/                      — point-in-time audits (unchanged)
    handoffs/                      — per-task delivery record (this file added; nothing else touched)
```

## Unresolved inconsistencies

1. **`AGENTS.md`/`docs/api/` untracked** (above) — a git-hygiene decision (stage and commit, or regenerate
   later) left to the maintainer, since committing was out of this task's scope.
2. **BLOCKER-2 from PF0 is still open**: `Lead` has no `location_id`, so a location-scoped principal cannot
   create a lead under the current permission model. PF0 recommends an additive nullable
   `leads.location_id`; no PF block has implemented it yet. Recorded in `docs/roadmap.md` under NEXT.
3. **The stale `backend-documentation-design.md` spec** (above) remains in `docs/superpowers/specs/` describing
   files that don't exist — intentionally not edited, per the append-only rule for that directory.

## Recommended next documentation maintenance rule

Every PF/task handoff that closes a block should end with one line stating which curated doc(s) it obsoletes
or extends (e.g., "extends `docs/architecture.md` §9 gap #3"), so `docs/architecture.md`,
`docs/backend-evolution.md`, and `docs/roadmap.md` get a small, immediate update instead of drifting until the
next dedicated documentation pass. Whoever authors a new spec under `docs/superpowers/specs/` should also
name, in the spec's own header, the exact files its "Scope" section promises to deliver — the mismatch found
in `backend-documentation-design.md` (scope named five files not under version-control review, only two were
committed) is exactly the failure mode this would catch.

---

STATUS: Documentation consolidation complete. No production code touched.
FILES_CHANGED: README.md (rewritten, trimmed); docs/README.md, docs/product-vision.md, docs/architecture.md, docs/backend-evolution.md, docs/roadmap.md (created); this handoff (created).
CURRENT_STATE: Vertical 1 CLOSED (4086dc1); PF1 CLOSED (4ff2de5); PF2 CLOSED (44ba874); PF3 CLOSED (1a737b0), scoped to booking/cancel/reschedule with four documented gaps; PF4 designed, not implemented; 274 tests collected at HEAD.
GITHUB_HYGIENE: No secrets/env/audit content tracked; .gitignore already sufficient, no change made. AGENTS.md and docs/api/{openapi.yaml,json} exist in the working tree but are NOT committed to git — flagged, not fixed (git-operation decision left to maintainer).
DOC_STRUCTURE: README.md (entry point) → docs/README.md (index) → product-vision.md / architecture.md / backend-evolution.md / roadmap.md (curated layer) → docs/superpowers/{specs,plans,evidence,handoffs} (unchanged, append-only engineering record).
CONTRADICTIONS_FOUND: backend-documentation-design.md spec names 5 files, only 2 exist; prior README overclaimed PF2/PF3 completeness by omitting 4 known gaps; AGENTS.md references docs/MIGRATION.md, which does not exist; BLOCKER-2 (Lead has no location_id) remains open from PF0.
HANDOFF: docs/superpowers/handoffs/2026-08-14-github-repository-consolidation-handoff.md
READY_TO_COMMIT: NO
