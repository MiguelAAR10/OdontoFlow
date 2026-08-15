# Economic & Operations Bridge — handoff

Date: 2026-08-15 · Authority: `.audit/clinical-core/next-economic-ops-contract.md`, legacy evidence `.audit/accelerator/ops-finance-legacy.md`
Base: Clinical Core (`ed8bcbc`) · Suite before: 324 passed

## What shipped (odontosmart-backend)

New `app/economics/` module (migration `0006_economic_ops`, additive; 8 permission codes + system grants):

- **Product** — org-owned catalog item: `name` unique per org, `unit`, **declared `kind` CHECK (consumible/reventa)** (the legacy inferred distinction becomes declared — defect #6 dropped); **no stock authority** (no `stock_actual`, no price — Inventory/Finance own those later). Duplicate-name race settles as a stable 422 (constraint-disambiguated).
- **ServiceConsumption** — anchored to one `ServiceExecution` and one canonical `Product` via §7 composite FKs; `UNIQUE(org, execution, product)` (one product per line, legacy rule); `quantity > 0`; `unit_price >= 0` **snapshot** frozen at use (line amount derived at read: `quantity × unit_price`); multiple consumptions per execution OK. **No InventoryMovement** in this activity (contract sequence defers it to the inventory vertical; consumption is clinical use, not a balance-change authority).
- **Charge** — the economic obligation of one execution: `UNIQUE(org, execution)` (1:1, legacy factura adapted to the execution line); `amount > 0`, **defaulting to the execution's own price snapshot** (never re-guessed); paid/outstanding **always derived**, never stored.
- **Payment** — N:1 per charge (multiple partial payments proven); `amount > 0`; `method` string (method catalog deferred); **deterministic overpayment rejection** on the single authoritative path: the charge row is locked `FOR UPDATE` and the sum re-checked — concurrent payments serialize (proven with threads + Barrier). Payments are append-only: no `delete-orphan`, FK RESTRICT, reversal-not-delete.
- **PF gap fix**: `create_organization` now provisions system access atomically (PR7) — runtime organizations are immediately operable by the platform actor (proven; authorization tests updated to the new reality).

Reuse, no duplication: PF1 composite FKs/`scoped`, PF2 `require_permission` (ctx-gated) on all reads/creates, PF3 audit atomic per mutation, PF4 claim-first idempotency on all four creates (shared claim/settle helpers) with replay rendered from stored outcome.

## API (27 OpenAPI paths; economic/ops = 8)

`POST/GET /products` (+search/kind) · `GET /products/{id}` · `POST/GET /executions/{id}/consumptions` ·
`POST /executions/{id}/charges` · `GET /charges?execution_id=` · `GET /charges/{id}` (derived paid/outstanding) ·
`POST/GET /charges/{id}/payments`.

## Proofs (tests/test_economic_ops.py — 20 tests, real PostgreSQL)

Cross-tenant product rejection (app + FK), duplicate product name sequential + **concurrent race → 422**; kind/search; consumption quantity/price validation (app + CHECK backstop); unit-price snapshot immutability; multiple consumptions + duplicate rule (sequential, **concurrent → 422**, DB backstop, other-execution reuse); cross-tenant consumption rejected by composite FK; charge 1:1 + amount from execution snapshot; charge tenant isolation; partial/full payment with derived state; overpayment rejected (sequential + **concurrent via row lock**); payment tenant isolation; permissions for all four commands; audit provenance; PF4 idempotency (4 commands × replay, one receipt per key); **runtime system-access provisioning**; payments append-only at the ORM level. Migration upgrade/downgrade/re-upgrade via `test_migrations.py` (HEAD `0006`).

## Test runs

- Focused: `tests/test_economic_ops.py` → **20 passed** (3× runs, no flake).
- Full suite: `.venv/bin/python -m pytest -q` → **344 passed** (324 + 20).
- Adapted drift: `test_authorization.py` (catalog now 31 codes; runtime-org system grant now True — the gap fix), `test_lead.py`, `test_migrations.py` (HEAD `0006`), `test_tenant_integrity.py` (4 new tenant tables), `conftest.py` (truncation list).
- OpenAPI regenerated and verified (27 paths).

## Frontend (odontosmart-frontend, second writer allowed for the separate repo)

Patients screen integrated with the real Clinical API:
- OpenAPI-generated TypeScript contracts (`src/contracts/api.ts` regenerated).
- `src/api.ts`: real-mode `loadPatients`/`createPatient` — backend `PatientRead` mapped to the UI view model (`toUiPatient`), creation with per-intent `Idempotency-Key`, no invented Patient fields.
- `PatientsPage`: loading + error states (banner); visual design kept; other screens remain on mocks; Agenda untouched.
- Proofs: typecheck clean · `npm test` **33 passed** · `npm run build` OK · **E2E 6/6 against live FastAPI + PostgreSQL** (`VITE_USE_MOCKS=false`): agenda (3) + patients (3 — list, create+replay exactly-once, envelope error mapping).

## Independent review (ONE read-only DeepSeek V4 Flash via the validated runner)

VERDICT: **PASS**, no blockers; 3 ISSUEs — all resolved in the single repair pass:
1. Product duplicate-name race surfaced raw `23505` → constraint-disambiguated to stable 422 (consistent with consumption/charge) + concurrent test.
2. Money guard documented deviation (row lock vs declarative CHECK — a CHECK on a derived sum is not expressible in DDL): recorded in `app/economics/README.md` and this handoff; the single authoritative mutation path is the deterministic guard.
3. `Charge.payments` retained `delete-orphan` → removed (append-only; RESTRICT) + ORM-level test.

## Out of scope (untouched)

InventoryMovement/InventoryBalance (next contract), Invoice/discount engine, payment reversal, method catalog, multi-location stock, OLAP, frontend Finance/Inventory screens.

## Docs

`app/economics/README.md` (module card) · `docs/architecture.md` · `docs/roadmap.md` (PF6 DONE; inventory NEXT) · CHANGELOG · parallel scout evidence `.audit/economic-ops/next-inventory-contract.md`.

## Risks / notes

- The overpayment invariant depends on the single mutation path (row lock); any future writer of payment rows must use the service (documented).
- The session-expiry/autobegin trap continues to be handled per-test (ids captured before rollbacks; reads closed with rollback).
- `next-inventory-contract.md` (scout, 7 tool calls) defines the next vertical: `InventoryMovement` append-only ledger → derived `InventoryBalance`, movement types for V1, consumption→SALIDA relationship in the same transaction, one authoritative mutation path.

## Economic & Operations Bridge: CLOSED
