# Backend Architecture Documentation + GitHub Consolidation — Handoff

**Date:** 2026-08-14 · **Nature:** DOCUMENTATION ONLY. No `app/`, `alembic/`, `tests/`, or dependency file was
changed. `git diff --stat -- app/ alembic/ tests/ pyproject.toml docker-compose.yml` is empty. This handoff
extends the same-day `docs/superpowers/handoffs/2026-08-14-github-repository-consolidation-handoff.md` with a
deeper technical blueprint and a targeted MediStock domain inspection; it does not redo that handoff's
GitHub-hygiene work, only re-verifies it.

## Backend current state (re-verified from Git, not assumed)

- `git log --oneline -5` on `main`: `11d2ad6`, `f7b2b0d`, `82d477e`, `1a737b0`, `44ba874` — unchanged since the
  prior handoff; no new commits landed between the two documentation passes.
- `alembic/versions/`: exactly `0001_lead_to_appointment.py`, `0002_organization_tenant_integrity.py`,
  `0003_principal_authorization.py`. No `0004`.
- `grep -rn "CommandReceipt\|command_receipt\|idempotency_key" app/` returns nothing — **PF4 is confirmed
  absent from `HEAD`**, not merely "not yet checked."
- Vertical 1 CLOSED (`4086dc1`); PF1 CLOSED (`4ff2de5`); PF2 CLOSED (`44ba874`); PF3 CLOSED (`1a737b0`) but
  scoped to booking/cancel/reschedule only, with four documented compatibility gaps (no authentication;
  `provision_system_access` not wired into `create_organization`; three-endpoint scope; `ctx=None`
  compatibility path) — all four traced to specific code/spec citations in
  `docs/backend-platform-blueprint.md` §7.4.
- `pytest --collect-only -q` collects 274 tests at `HEAD`, matching the PF3 handoff's final count.

## Architecture summary

Documented in full in `docs/backend-platform-blueprint.md`: a FastAPI modular monolith over PostgreSQL, six
module boundaries (`commercial`, `catalog`, `organization`, `scheduling`, `iam`, `audit`), with tenant
integrity, the practitioner-global overlap invariant, and audit atomicity all enforced at the database, not
only in application code. Each of the twelve architectural principles named in this task's brief is
documented with the concrete mechanism *and* the reasoning behind it (§2 of the blueprint), not just restated
as a bullet.

## Documentation created/updated (this pass)

| File | Action | Purpose |
|---|---|---|
| `docs/backend-platform-blueprint.md` | **Created** | Detailed technical authority: principles+why, module architecture + Mermaid diagram (HTTP→Router→ExecutionContext/Authorization→Service→Domain/Data→PostgreSQL→Audit, implemented vs. planned marked), full Lead→Appointment lifecycle, PF1–PF4 rationale, MediStock migration map with ADAPT/REFERENCE/DEFER/DROP classification and a detailed Inventory finding, legacy-surfaces-not-migrated list. |
| `docs/product-vision.md` | **Extended** | Added: target ERP bounded-context table, target domain spine diagram (`Lead → Appointment → Patient → Visit → ServiceExecution → {Charge→Payment→Invoice}` and `→ {ServiceConsumption→InventoryMovement→InventoryBalance/Transfer}`), operational-intelligence vision (explicitly no trained world model, no fine-tuning/RAG/LoRA prescribed), integration architecture (Calendar/WhatsApp/Email/billing/voice-STT as adapters, OdontoFlow as source of truth). |
| `docs/architecture.md` | **Light edit** | One line pointing to the new blueprint for depth. |
| `docs/backend-evolution.md` | **Light edit** | Milestone table gained an explicit `Status` column and an explicit PF4 row (`PENDING — verified absent`). |
| `docs/roadmap.md` | **Light edit** | One line pointing to the blueprint for *why* the PF1→PF4 sequencing exists. |
| `docs/README.md` | **Light edit** | Indexed the new blueprint. |
| `README.md` | **Light edit** | One documentation-table row added for the blueprint. |
| `docs/superpowers/handoffs/2026-08-14-backend-github-consolidation-handoff.md` | **Created** | This file. |

No file under `docs/superpowers/{specs,plans,evidence}/` was moved, renamed, or edited. The prior handoff
(`2026-08-14-github-repository-consolidation-handoff.md`) was left as-is, not amended.

## MediStock migration map (summary — full detail + evidence in `backend-platform-blueprint.md` §10)

A targeted, read-only inspection of exactly ten domain concepts plus inventory mechanics — not a broad audit —
covering `src/clinica_backend/app/models/{paciente,servicio,servicio_catalogo,consulta,consulta_servicio,
producto,consumo_producto,inventario,factura,pago,medio_pago,descuento}.py`,
`src/clinica_backend/app/services/{inventario,factura,pago,consulta}_service.py`, and
`src/sql/migrations/00{1,2,3}_*.sql`.

| Concept | Classification |
|---|---|
| Paciente → Patient | ADAPT (must become organization-owned directly, per PF0 P10) |
| Servicio/ServicioCatalogo → Service | REFERENCE (already implemented differently; MediStock has two model classes mapped to the same table — a legacy duplication OdontoFlow's single canonical `Service` is designed to prevent) |
| Consulta → Visit | ADAPT |
| ConsultaServicio → ServiceExecution | ADAPT (the price-snapshot-at-execution pattern is worth keeping) |
| Producto → Product | ADAPT, with a correction (see Inventory finding) |
| ConsumoProducto → ServiceConsumption | ADAPT |
| **Inventory** (`MovimientoStock` + triggers + `InventarioService`) → InventoryMovement/InventoryBalance | **ADAPT the ledger idea; DROP the mutable-mirror mechanism** — see finding below |
| Factura → Invoice | ADAPT (must attach to a `ServiceExecution`, not directly to a `Consulta`) |
| Pago → Payment | ADAPT (hard-delete-as-void must become an explicit reversal) |
| MedioPago → PaymentMethod | ADAPT (directly portable, no changes needed) |
| Descuento → PricingAdjustment/Discount | ADAPT, narrow the model (MediStock applies it only at the invoice level — a limitation to decide about deliberately, not inherit silently) |

**The Inventory finding, in one paragraph (full evidence with file:line in the blueprint):** MediStock has a
real append-only stock ledger (`movimientos_stock`, DB-trigger-populated from every `ConsumoProducto` insert)
— a genuinely good pattern — but a *second* trigger denormalizes that ledger onto a mutable
`productos_catalogo.stock_actual` column, and the actual application code path
(`InventarioService.registrar_consumo`) **also directly decrements that same column in Python**, so the
column is written twice per consumption with the final value depending on statement ordering, not
correctness. A separate `ajustar_stock` path adjusts stock with **no ledger entry at all**. This is concrete,
evidence-based confirmation of exactly the anti-pattern `product-vision.md` already commits to avoiding
("ledger-based... never a direct mutable-stock decrement") — it is not a new policy invented for this
handoff, it is the legacy failure that policy already existed to prevent, now documented with proof.

Legacy surfaces explicitly excluded from migration (per the task brief, confirmed present in the repo, not
touched): Flask routes, Marshmallow schemas, the Streamlit UI (`src/clinica_frontend/`), legacy LangGraph
agent orchestration (`src/clinica_backend/app/agents/*`), the notebook runtime, and OLAP V1
(`src/jobs/{run_olap_cycle,setup_olap}.py`, `src/sql/olap/`).

`../medistock` was not modified. No file outside the ten named domain concepts and the inventory
trigger/service chain was read for this map.

## ERP future module map (summary — full detail in `product-vision.md`)

Seven bounded contexts (Commercial/CRM, Scheduling, Clinical, Finance, Inventory/Operations, Integrations,
Intelligence/Optimization); only the first two are implemented. Target domain spine documented and clearly
marked FUTURE. The one rule restated in both the blueprint and product-vision: one canonical `Service` row
serves every vertical — no duplicate catalogs, learned directly from the MediStock `Servicio`/
`ServicioCatalogo` duplication above.

## Implemented vs. planned (no ambiguity)

**Implemented:** Vertical 1 (Lead→Appointment, closed), PF1 (tenant integrity), PF2 (permission-based IAM),
PF3 (ExecutionContext + audit provenance, scoped to 3 endpoints, 4 documented gaps).
**Planned, not implemented:** PF4 (idempotent commands — no code), Clinical Bridge, Finance, Inventory/
Operations, Integrations, Intelligence/Optimization. Neither PF4 nor Clinical Bridge was implemented as part
of this task, per the brief.

## GitHub hygiene (re-verified, not re-audited from scratch)

- No secrets, credentials, `.env`, or `.audit/` content tracked in git (`git grep` for AWS-key/private-key/
  embedded-credential patterns over tracked files: empty). `.gitignore` already covers `.venv/`,
  `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.env`, `*.egg-info/`, `dist/`, `build/`, `.DS_Store`,
  `.audit/`. No change made — none needed.
- The only stray log-like file found in the working tree is `.audit/pf3/pytest.log`, and it is correctly
  ignored (`.audit/` pattern) — not tracked, not a hygiene issue.
- **Still true, restated:** `AGENTS.md` and `docs/api/{openapi.yaml,json}` exist in the working tree but are
  **not committed to git** (`git status` shows both `??`). A fresh clone would be missing them. This was
  identified in the prior handoff and remains true; it was not fixed here (a git-staging decision, not a
  documentation one, and out of this task's explicit scope).

## Tracked/untracked issues

Tracked correctly: all of `app/`, `alembic/versions/`, `tests/`, `pyproject.toml`, `docker-compose.yml`,
`.env.example`, and every pre-existing `docs/superpowers/` file. **Untracked and worth a maintainer decision:**
`AGENTS.md`, `docs/api/openapi.yaml`, `docs/api/openapi.json` (pre-existing gap, not introduced by this
session). **Newly untracked** (this session's output, expected — nothing has been committed per instruction):
all files listed in the "Documentation created/updated" table above, plus the prior handoff.

## Secrets check

No secret values were printed or logged anywhere in this process. `git grep` for AWS access-key patterns,
PEM/OpenSSH private-key headers, and embedded `user:pass@host` credential strings across all tracked files
returned nothing.

## Push readiness

- `git remote -v`: `origin` → `git@github.com:MiguelAAR10/OdontoFlow.git` (fetch + push). Not invented —
  pre-existing.
- Current branch `main` tracks `origin/main` and is **already 2 commits ahead** of what's on GitHub
  (`f7b2b0d`, `11d2ad6` — pre-dating this session, never pushed).
- `git merge-base --is-ancestor origin/main HEAD` → true: a **fast-forward push is safe**, no force needed,
  no divergence to reconcile.
- The working tree currently has the uncommitted documentation changes listed above. **Nothing was
  committed in this session**, per instruction ("Do NOT commit unless explicitly instructed by the
  orchestrator").
- **Exact command to push, once someone commits this work:** `git push origin main` — a plain fast-forward
  push to the existing tracked branch and the existing remote. No new remote or branch is proposed.

**PUSH_READY: NO** — not because anything is unsafe, but because (a) this session's documentation changes
are intentionally uncommitted per the task's own instruction, and (b) even the pre-existing 2-commit gap
against `origin/main` predates this session and was left for the maintainer to commit/push deliberately.

## Unresolved architecture gaps

1. The four PF3 compatibility gaps (§7.4 of the blueprint) — no authentication; `create_organization` doesn't
   call `provision_system_access`; context/permission enforcement scoped to 3 endpoints; `ctx=None`
   compatibility path.
2. PF0's BLOCKER-2 (`Lead` has no `location_id`) — still open, still blocks a location-scoped principal from
   creating a lead.
3. PF4 has zero code at `HEAD`.
4. `AGENTS.md`/`docs/api/` untracked (GitHub hygiene, not architecture, but unresolved either way).
5. The MediStock inventory double-write pattern is now documented as a **finding to design around**, not a
   defect anyone needs to fix in MediStock — MediStock stays read-only and untouched.

## Next development activity

Per this task's explicit exclusion, **not** PF4 and **not** Clinical Bridge implementation. The next
*documentation-adjacent* activity, if one is wanted: decide and stage the `AGENTS.md`/`docs/api/` tracking gap
(commit them or regenerate `docs/api/` fresh before committing, maintainer's call). The next *engineering*
activity, per the roadmap this documentation describes but does not perform, is PF4 (idempotent commands),
followed by Platform Foundation gap closure (item 1 above) before Clinical Bridge begins.

---

STATUS: Backend architecture documentation + GitHub consolidation complete. No production code touched.
BACKEND_STATE: Vertical 1 CLOSED (4086dc1); PF1 CLOSED (4ff2de5); PF2 CLOSED (44ba874); PF3 CLOSED (1a737b0, scoped, 4 documented gaps); PF4 confirmed absent (no migration 0004, no CommandReceipt code). 274 tests collected at HEAD.
DOCS: Created docs/backend-platform-blueprint.md; extended docs/product-vision.md (ERP bounded contexts, domain spine, operational-intelligence vision, integration architecture); light-edited docs/architecture.md, docs/backend-evolution.md, docs/roadmap.md, docs/README.md, README.md; created this handoff.
MEDISTOCK_MIGRATION: 10 domain concepts classified (mostly ADAPT, one REFERENCE); detailed evidence-based Inventory finding (double-write stock bug: DB trigger + app code both mutate stock_actual, plus a ledger-bypassing manual-adjustment path); legacy surfaces (Flask routes, Marshmallow, Streamlit UI, LangGraph agents, notebooks, OLAP V1) explicitly excluded from migration. ../medistock untouched.
ERP_MAP: 7 bounded contexts (Commercial/CRM, Scheduling implemented; Clinical, Finance, Inventory/Operations, Integrations, Intelligence/Optimization FUTURE); target domain spine documented; single-canonical-Service rule stated.
GITHUB_HYGIENE: No secrets/credentials/.env/.audit content tracked; .gitignore sufficient, unchanged. AGENTS.md and docs/api/{openapi.yaml,json} remain untracked (pre-existing gap, re-confirmed, not fixed — maintainer decision).
PUSH_READY: NO — fast-forward to origin/main is safe (verified via merge-base), exact command is `git push origin main`, but nothing was committed this session per instruction and a pre-existing 2-commit gap against origin/main predates this work.
BLOCKERS: PF3's 4 compatibility gaps; PF0 BLOCKER-2 (Lead has no location_id); PF4 not started; AGENTS.md/docs/api/ untracked.
HANDOFF: docs/superpowers/handoffs/2026-08-14-backend-github-consolidation-handoff.md
NEXT: Engineering — PF4 (idempotent commands), then Platform Foundation gap closure, then Clinical Bridge. Documentation — none required immediately; maintainer should decide on the AGENTS.md/docs/api/ tracking gap before the next push.
