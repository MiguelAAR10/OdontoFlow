# Inventory Ledger + PF Closure — handoff

Date: 2026-08-16 · Authority: `.audit/economic-ops/next-inventory-contract.md`, PF0 spec §15–16
Base: Economic & Operations Bridge (`4428b72`) · Suite before: 344 passed

## What shipped (odontosmart-backend)

**PF7 — Inventory ledger (`app/inventory/`, migration `0007_inventory`):**
- `InventoryMovement` — the **only** stock truth, append-only: ENTRADA (purchase/input, legacy SP adapted to a real HTTP surface), SALIDA (consumption-linked), ADJUSTMENT (reason-required correction — the legacy `ajustar_stock` silent-write hole closed). Per-type CHECKs (quantity > 0 for ENTRADA/SALIDA; <> 0 for ADJUSTMENT; reason mandatory on ADJUSTMENT). No location dimension (legacy has none; transfers deferred). Composite FKs (§7) to `products` and `service_consumptions`; `UNIQUE(id_consumo_origen)` keeps consumption↔SALIDA 1:1.
- **Derived `InventoryBalance`** — read-time aggregate (`Σ ENTRADA − Σ SALIDA + Σ signed ADJUSTMENT`); **no `stock_actual` column, no trigger cache** (the trigger-cache mechanism that produced the legacy dual-writer defects is rejected); the kardex is an ordered ledger query.
- **Consumption→SALIDA** — `create_service_consumption` now emits the SALIDA movement in the same `session.begin()` (1:1 via `id_consumo_origen`), with the **negative-balance guard** (`require_stock`: product row `FOR UPDATE` + ledger SUM — the single authoritative stock-floor path, proven under concurrency). A DB trigger rejects any SALIDA whose product differs from the referenced consumption's product (structural causality).
- New API (31 OpenAPI paths total; inventory = 4): `POST /products/{id}/entries`, `POST /products/{id}/adjustments` (both PF4-idempotent), `GET /products/{id}/movements` (kardex), `GET /products/{id}/balance` (derived) — `movements.read/create` permissions, PF3 audit.
- **PF closure**: every remaining mutating service is now ctx-gated permission-checked — `create_lead` (LEADS_CREATE, **org-wide only → BLOCKER-2 resolved**, E5), `create_service` (SERVICES_MANAGE), `create_location` (LOCATIONS_MANAGE), `create_practitioner` (PRACTITIONERS_MANAGE), `add_practitioner_membership` (PRACTITIONERS_MANAGE, org-wide), `create_capability` (CAPABILITIES_MANAGE, location-scoped honored), `create_availability_rule`/`create_schedule_block` (AVAILABILITY_MANAGE, location-scoped honored). Routers pass `resolve_http_context`.

## Proofs (real PostgreSQL)

- `tests/test_inventory.py` — **16 tests**: entries + derived balance; no stock column exists (schema proof); per-type CHECKs; signed adjustments with reason; negative adjustment without stock rejected; cross-tenant movements rejected (FK) + cross-org balance NOT_FOUND; consumption→SALIDA same-tx with 1:1 linkage; insufficient-stock rejection; **concurrent consumptions never overdraw** (threads + Barrier: 5 − 4 = 1, never negative); SALIDA product-mismatch rejected by trigger; duplicate provenance key rejected; permissions; audit provenance; PF4 idempotency (entries + adjustments replay exactly-once); HTTP journey with 422s.
- `tests/test_pf_closure.py` — **4 tests**: BLOCKER-2 (org-wide vs location-scoped lead grants), catalog gate, the five organization/scheduling gates, location-scoped grant honored.
- Updated PF6 tests: consumption now requires prior ENTRADA (stock floor).
- Full suite: **364 passed** (344 + 16 + 4), migration cycle to HEAD `0007` incl. the trigger (upgrade/downgrade/re-upgrade), tenant-integrity enumeration, catalog now 33 codes.
- OpenAPI regenerated and verified (31 paths).

## Independent review (ONE read-only DeepSeek V4 Flash via the validated runner)

VERDICT: **PASS**, no blockers; 3 ISSUEs — all resolved in the single repair pass:
1. SALIDA product causality rested only on the app path → **DB trigger** `trg_inventory_movements_salida_product` rejects mismatched pairings + test.
2. `add_practitioner_membership` was an un-gated mutating service → ctx-gated (PRACTITIONERS_MANAGE, org-wide) + test.
3. CHANGELOG missing the PF7/closure entry → added.

## Out of scope (untouched)

Sale↔invoice linkage, multi-location stock/transfers, purchase orders/unit-cost, reason catalog, kardex OLAP, frontend inventory screens.

## Docs

`app/inventory/README.md` (module card) · `app/economics/README.md` (consumption→SALIDA semantics) · `docs/architecture.md` · `docs/roadmap.md` (PF7 + closure DONE) · CHANGELOG.

## Risks / notes

- The negative-balance guard lives on the single authoritative path (row lock + ledger sum); any future stock writer must use the same service (documented in the module card).
- The SALIDA trigger keeps causality structural; the composite FK alone cannot compare across tables.
- Per-type CHECKs and the trigger are migration-owned; a future TRANSFER type is purely additive.

## Inventory Ledger + PF Closure: CLOSED
