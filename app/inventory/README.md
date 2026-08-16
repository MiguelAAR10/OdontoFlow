# app/inventory — Inventory Ledger

**Metadata:** module `inventory` · migration `0007_inventory` · PF7 · owns `InventoryMovement` (append-only ledger) and the derived `InventoryBalance`.

## Purpose

The single source of stock truth as an append-only movement journal, per
`.audit/economic-ops/next-inventory-contract.md`. The balance is a derived
read-time aggregate — no `stock_actual` column, no trigger cache, one
authoritative mutation path.

## Owns

- `InventoryMovement` — ENTRADA (purchase/input), SALIDA (consumption-linked), ADJUSTMENT (reason-required correction).
- `InventoryBalance` — derived (`Σ ENTRADA − Σ SALIDA + Σ signed ADJUSTMENT`), never stored.

## Inputs / Outputs

- **Inputs:** HTTP → Pydantic schemas (`extra="forbid"`) → services with explicit `ExecutionContext` + idle `Session`.
- **Outputs:** typed read DTOs (`MovementRead` kardex, `BalanceRead`).

## Dependencies

- `app/economics` — `Product` (org-owned catalog; the ledger keys on it).
- `app/economics` (cross-vertical) — `create_service_consumption` emits the SALIDA movement in the same transaction.
- `app/iam` — `movements.read` / `movements.create` permissions.
- `app/idempotency` — claim-first PF4 on entries and adjustments.
- `app/audit` — atomic provenance (PF3).

## Invariants

- Append-only: no UPDATE/DELETE on movement rows; corrections are new rows with a reason; reversal is an offsetting movement.
- `type IN ('ENTRADA','SALIDA','ADJUSTMENT')`; `quantity > 0` for ENTRADA/SALIDA, `<> 0` for ADJUSTMENT; `reason` mandatory on ADJUSTMENT (all CHECK-enforced).
- No location dimension (org-level stock; legacy has none; transfers deferred).
- Consumption↔SALIDA 1:1 via `UNIQUE(id_consumo_origen)` **and** a DB trigger that rejects a SALIDA whose product differs from the referenced consumption's product.
- Negative-balance guard: `require_stock` locks the product row `FOR UPDATE` and sums the ledger — the single authoritative stock-floor path (concurrency-proof).
- Composite FKs (§7) into products and service_consumptions; all RESTRICT; integer Identity PKs; timestamptz; Decimal end-to-end.

## Public surface

| Method | Path | Permission | Idempotency |
|---|---|---|---|
| POST | `/products/{id}/entries` | `movements.create` | Yes |
| POST | `/products/{id}/adjustments` | `movements.create` | Yes |
| GET | `/products/{id}/movements` | `movements.read` | — |
| GET | `/products/{id}/balance` | `movements.read` | — |

(Consumption → SALIDA rides on `POST /executions/{id}/consumptions`, `consumptions.create`.)

## Data

`products` ← 1:N `inventory_movements`; `service_consumptions` ← 1:1 (via `id_consumo_origen`) movements of type SALIDA. Legacy mapping: `movimientos_stock` (ledger, triggers 001/002) → `InventoryMovement`; the six stock-write authorities and direct `stock_actual` writes are **dropped**; ENTRADA SP → the `entries` endpoint.

## Tests

`tests/test_inventory.py` (16 tests): entries + derived balance; no stock column exists; per-type CHECKs (quantity/type/reason); adjustment signed + reason; negative adjustment without stock rejected; cross-tenant movements rejected (FK) and cross-org balance NOT_FOUND; consumption emits SALIDA in the same tx with 1:1 linkage; insufficient-stock rejection; **concurrent consumptions never overdraw** (Barrier); SALIDA product-mismatch rejected by trigger; duplicate provenance key rejected; permissions; audit provenance; PF4 idempotency (entries + adjustments, replay exactly-once); HTTP journey (entry/adjustment/balance/kardex, 422s).

## Next

- Sale stock-out for `kind='reventa'` (movement exists; invoice linkage deferred).
- Multi-location stock & transfers (additive later: `location_id` + TRANSFER type).
- Kardex reporting/OLAP; reason catalog; purchase orders/unit-cost.
