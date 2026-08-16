# app/inventory — Inventory Ledger

**Metadata:** module `inventory` · migration `0008_location_aware_inventory` · PF7 + M4.2 · owns `InventoryMovement` (append-only ledger) and the derived `InventoryBalance`.

## Purpose

The single source of stock truth as an append-only movement journal, per
`.audit/economic-ops/next-inventory-contract.md`. The balance is a derived
read-time aggregate per Product × Location — no `stock_actual` column, no
trigger cache, one authoritative mutation path.

## Owns

- `InventoryMovement` — ENTRADA (purchase/input), SALIDA (consumption-linked), ADJUSTMENT (reason-required correction), TRANSFER_OUT / TRANSFER_IN (paired transfer rows sharing `transfer_id`).
- `InventoryBalance` — derived (`Σ ENTRADA + Σ TRANSFER_IN − Σ SALIDA − Σ TRANSFER_OUT + Σ signed ADJUSTMENT`), never stored.
- `transfer_product` — one atomic stock-conserving transfer command.

## Inputs / Outputs

- **Inputs:** HTTP → Pydantic schemas (`extra="forbid"`) → services with explicit `ExecutionContext` + idle `Session`.
- **Outputs:** typed read DTOs (`MovementRead` kardex, `BalanceRead`, `TransferRead`).

## Dependencies

- `app/economics` — `Product` (org-owned catalog; the ledger keys on it).
- `app/organization` — `Location` (org-owned; the ledger keys on it too).
- `app/economics` (cross-vertical) — `create_service_consumption` emits the SALIDA movement in the same transaction, at the execution's visit location.
- `app/iam` — `movements.read` / `movements.create` permissions.
- `app/idempotency` — claim-first PF4 on entries, adjustments and transfers.
- `app/audit` — atomic provenance (PF3).

## Invariants

- Append-only: no UPDATE/DELETE on movement rows; corrections are new rows with a reason; reversal is an offsetting movement.
- `type IN ('ENTRADA','SALIDA','ADJUSTMENT','TRANSFER_OUT','TRANSFER_IN')`; `quantity > 0` for ENTRADA/SALIDA/TRANSFER_*, `<> 0` for ADJUSTMENT; `reason` mandatory on ADJUSTMENT (all CHECK-enforced).
- Every movement carries `location_id` (NOT NULL, composite FK into `locations(organization_id, id)`); the balance is per `(organization_id, product_id, location_id)` — stock at other locations is never affected.
- Transfers: exactly-one-Out and exactly-one-In per `transfer_id` (partial unique indexes) and the pair must share organization, product and quantity and move between distinct locations (deferred constraint trigger, validated at COMMIT) — a partial or inconsistent pair cannot commit.
- Consumption↔SALIDA 1:1 via `UNIQUE(id_consumo_origen)` **and** a DB trigger that rejects a SALIDA whose product differs from the referenced consumption's product; the SALIDA location is the execution's visit location (never client-supplied).
- Negative-balance guard: `require_stock` locks the product row `FOR UPDATE` and sums the ledger of the target location — the single authoritative stock-floor path (concurrency-proof across consumptions, transfers and negative adjustments).
- Composite FKs (§7) into products, locations and service_consumptions; all RESTRICT; integer Identity PKs; timestamptz; Decimal end-to-end.

## Public surface

| Method | Path | Permission | Idempotency |
|---|---|---|---|
| POST | `/products/{id}/entries` | `movements.create` | Yes |
| POST | `/products/{id}/adjustments` | `movements.create` | Yes |
| POST | `/products/{id}/transfers` | `movements.create` | Yes |
| GET | `/products/{id}/movements?location_id=` | `movements.read` | — |
| GET | `/products/{id}/balance?location_id=` | `movements.read` | — |

(Consumption → SALIDA rides on `POST /executions/{id}/consumptions`, `consumptions.create`.)

## Data

`products` ← 1:N `inventory_movements`; `locations` ← 1:N `inventory_movements`; `service_consumptions` ← 1:1 (via `id_consumo_origen`) movements of type SALIDA. Legacy mapping: `movimientos_stock` (ledger, triggers 001/002) → `InventoryMovement`; the six stock-write authorities and direct `stock_actual` writes are **dropped**; ENTRADA SP → the `entries` endpoint; transfers are greenfield (no legacy semantics).

## Tests

`tests/test_inventory.py` + `tests/test_inventory_location.py` (33 tests): entries + derived balance; no stock column exists; per-type CHECKs; adjustments signed + reason; negative adjustment without stock rejected; cross-tenant movements rejected (FK) and cross-org balance NOT_FOUND; Product × Location balance isolation; cross-org Location rejection; entries/adjustments per Location; consumption reduces stock at the execution Location only; insufficient stock at that location rejected; **concurrent consumption/transfer never overdraw** (Barrier); transfers preserve total quantity; transfer DB pair invariants; transfer idempotency/audit/permissions; consumption emits SALIDA in the same tx with 1:1 linkage; SALIDA product-mismatch rejected by trigger; permissions; PF4 idempotency; HTTP journeys (entry/adjustment/transfer/balance/kardex, 422s). Migration cycle proven in `tests/test_migrations.py` (backfill truth, fail-loud, downgrade/re-upgrade).

## Next

- Sale stock-out for `kind='reventa'` (movement exists; invoice linkage deferred).
- Kardex reporting/OLAP; reason catalog; purchase orders/unit-cost; supplier semantics.
