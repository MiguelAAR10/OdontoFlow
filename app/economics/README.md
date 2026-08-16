# app/economics — Economic & Operations Core

**Metadata:** module `economics` · migration `0006_economic_ops` · PF6 · owns Product, ServiceConsumption, Charge, Payment.

## Purpose

The minimum economic + operational domain anchored on the clinical execution
line: the organization-owned product catalog, actual clinical product use
(consumption), and the charge/payment pair with derived economic state. Domain
migration, not a full Finance or Inventory system.

## Owns

- `Product` — org-owned catalog item with declared kind (consumible/reventa), **no stock authority**.
- `ServiceConsumption` — one product actually used during one executed service.
- `Charge` — the economic obligation of one executed service (1:1).
- `Payment` — one payment against a Charge (N:1), deterministic overpayment rejection.

## Inputs / Outputs

- **Inputs:** HTTP → Pydantic schemas (`extra="forbid"`) → services with explicit `ExecutionContext` + idle `Session`.
- **Outputs:** typed read DTOs; monetary reads expose **derived** amounts only (paid/outstanding are never stored).

## Dependencies

- `app/clinical` — `ServiceExecution` is the anchor every row keys on (composite FKs).
- `app/iam` — 8 permission codes (`products.*`, `consumptions.*`, `charges.*`, `payments.*`), `require_permission`.
- `app/idempotency` — claim-first PF4 on all four creates.
- `app/audit` — atomic provenance (PF3).
- `app/catalog` — indirectly: the execution carries the canonical service reference.

## Invariants

- `products`: `UNIQUE(org, name)`; `kind IN ('consumible','reventa')` CHECK; no stock/price columns.
- `service_consumptions`: composite FKs (org, execution) and (org, product); `UNIQUE(org, execution, product)` (one product per line); `quantity > 0`; `unit_price >= 0` snapshot; line amount derived (`quantity × unit_price`). Consumption is real clinical use with a **stock floor**: the inventory ledger (PF7) must cover the quantity, and the SALIDA movement lands atomically in the same transaction (1:1 via `id_consumo_origen`).
- `charges`: `UNIQUE(org, execution)` (one charge per execution); `amount > 0`; defaults to the execution's price snapshot (never re-guessed).
- `payments`: composite FK (org, charge); `amount > 0`; append-only (no delete-orphan; FK RESTRICT; reversal, never delete); **overpayment structurally impossible**: the authoritative path locks the charge row `FOR UPDATE` and re-checks the sum — concurrent payments serialize deterministically. Documented deviation: the money rule lives on the single mutation path, not a declarative CHECK (a CHECK against a derived sum is not expressible in DDL; a second writer must use the same service).
- All FKs RESTRICT; integer Identity PKs; timestamptz; Decimal end-to-end.

## Public surface

| Method | Path | Permission | Idempotency |
|---|---|---|---|
| POST | `/products` | `products.create` | Yes |
| GET | `/products?search=&kind=` · `/products/{id}` | `products.read` | — |
| POST | `/executions/{id}/consumptions` | `consumptions.create` | Yes |
| GET | `/executions/{id}/consumptions` | `consumptions.read` | — |
| POST | `/executions/{id}/charges` | `charges.create` | Yes |
| GET | `/charges?execution_id=` · `/charges/{id}` | `charges.read` | — |
| POST | `/charges/{id}/payments` | `payments.create` | Yes |
| GET | `/charges/{id}/payments` | `payments.read` | — |

## Data

`service_executions` ← 1:N `service_consumptions` → N:1 `products` ·
`service_executions` ← 1:1 `charges` → 1:N `payments`. Legacy mapping:
producto→Product (declared kind), consumo_productos→ServiceConsumption,
factura 1:1 consulta→Charge per execution, pagos N:1→Payment
(see `.audit/accelerator/ops-finance-legacy.md` and
`.audit/clinical-core/next-economic-ops-contract.md`).

## Tests

`tests/test_economic_ops.py` (20 tests): tenant isolation at service and DB
level, kind/search, quantity/price validations (app + CHECK backstop),
snapshot immutability, multiple consumptions + duplicate rule (sequential,
concurrent race, DB backstop), charge default from execution price + 1:1 rule,
partial/full payment with derived state, overpayment rejection (sequential +
concurrent via row lock), payment/charge tenant isolation, permissions, audit
provenance, PF4 idempotency (4 commands × replay, one receipt per key),
runtime system-access provisioning (PF gap fix), product-name race → 422,
payments append-only at the ORM level.

## Next

- Finance follow-ups (deferred): payment reversal, method catalog, Invoice/discount engine.
- Sale stock-out for `kind='reventa'` products (movement exists; invoice linkage deferred).
