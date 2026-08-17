# Rules & Permissions — OdontoFlow Backend

This document is the map of **non-negotiable domain rules** and the **permission catalog** every human
and every agent operates under. The rules are enforced by PostgreSQL constraints, application services, or
both — never by client convention.

## Non-negotiable rules

### 1. PostgreSQL is the final authority
Overlap conflicts, tenant integrity and value-domain rules are enforced by constraints, not application
checks alone. The proof cases are in the test suite (real races with two sessions + `Barrier`).

### 2. Duration is catalog-authoritative
`Service.duration_minutes` is the only source of an appointment's end. Clients can never supply
duration/end/state (schemas use `extra="forbid"`).

### 3. Slots are pure logic
`app/scheduling/availability.py` is stdlib-only, half-open `[start, end)`, on a 15-minute grid in the
location's IANA timezone. It never imports the DB or FastAPI.

### 4. Transaction ownership lives in services
Mutations call `session.begin()` **before any read**; routers never open transactions and never run
pre-transaction queries.

### 5. Audit is atomic with the mutation
`record_event` stages the audit row inside the caller's transaction. No BackgroundTasks, no separate
commits. Who did what, when, with which request/correlation id — every mutation.

### 6. Tenant integrity is structural
Cross-organization relational states are **impossible at the DB level**: composite foreign keys into
`UNIQUE(organization_id, id)` for every row carrying `organization_id + location_id/service_id/lead_id/...`.
A location from another org cannot be attached to a product, a movement, an appointment or a visit.

### 7. The practitioner-global GiST stays
`EXCLUDE (practitioner_id =, tstzrange &&) WHERE state='confirmed'` — a practitioner cannot be
double-booked across organizations. Never add organization to that key.

### 8. Explicit context, no magic
`ExecutionContext` (organization, principal, request_id, correlation_id) is an explicit parameter at
service boundaries. The HTTP adapter resolves it per request; the tenant never comes from a body/query.

### 9. Authorization is permission-based
Machine-readable codes via `RolePermission`. No `if role == "owner"` anywhere. The catalog:

| Code | Grants |
|---|---|
| `appointments.read/create/reschedule/cancel` | Agenda operations |
| `patients.read/create` | Clinical patient operations |
| `visits.read/create` | Attendance |
| `executions.read/create` | Service executions |
| `services.read/manage` | Catalog |
| `leads.read/create` | Commercial |
| `locations.read/manage` | Branches |
| `practitioners.read/manage` | Practitioners |
| `capabilities.read/manage` | Eligibility grants |
| `availability.read/manage` | Rules + blocks |
| `products.read/create` | Product catalog |
| `consumptions.read/create` | Clinical consumption |
| `charges.read/create` | Charges |
| `payments.read/create` | Payments |
| `movements.read/create` | Inventory ledger + transfers |
| `audit.read` | Provenance |

(33 codes total; the `system` principal in the bootstrap org holds the full catalog — PR7.)

### 10. Stable error envelope
`{"error": {"code", "message", "details"}}` with the approved codes. Never leak SQL, constraint names,
SQLSTATE or stack traces. The six approved codes plus two HTTP-adjacent ones:

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_INPUT` | 422 | Validation / business-rule violation (e.g. overpayment, zero adjustment, missing reason) |
| `NOT_FOUND` | 404 | Missing or **cross-tenant** resource (no leakage: same code) |
| `ENTITY_INACTIVE` | 409 | Inactive service/location/member, non-confirmed appointment → visit |
| `CAPABILITY_MISSING` | 409 | Practitioner not eligible for service × location |
| `SLOT_BLOCKED` | 409 | Requested interval is not a bookable slot |
| `APPOINTMENT_CONFLICT` | 409 | GiST exclusion violation (double booking) |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same key, different payload |
| `PERMISSION_DENIED` | 403 | Deny-by-default authorization |

## Inventory ledger rules (M4)

1. **`inventory_movements` is the ONLY stock truth** — append-only. No `stock_actual`, no competing
   writer, no trigger cache. `InventoryBalance` is a derived read-time aggregate
   `Σ ENTRADA + Σ TRANSFER_IN − Σ SALIDA − Σ TRANSFER_OUT + Σ signed ADJUSTMENT` per
   `(organization_id, product_id, location_id)`.
2. **Every stock-affecting movement carries a `location_id`** with a composite FK
   `(organization_id, location_id) → locations(organization_id, id)` — cross-org or orphan locations are
   structurally impossible.
3. **Consumption location is derived, never supplied**: the SALIDA of a `ServiceConsumption` uses the
   `Visit.location_id` of its execution (1:1 causal link via `id_consumo_origen`, unique).
4. **Transfers are one atomic pair**: `TRANSFER_OUT` + `TRANSFER_IN` share a server-generated
   `transfer_id`, inserted in one transaction; exactly-one-Out/exactly-one-In per transfer (partial unique
   indexes); a `DEFERRABLE INITIALLY DEFERRED` trigger validates at COMMIT that the pair shares org,
   product and quantity and moves between distinct locations. A partial or inconsistent pair can never
   commit.
5. **Corrections are new rows with a reason** (ADJUSTMENT ≠ 0, reason required), never edits or deletes.
6. **Insufficient stock is rejected** at the origin (per-location floor check under a product-row
   `FOR UPDATE` lock).
7. **Migration `0008` never fabricates locations**: backfill derives consumption-linked SALIDA locations
   from their visit chain; any remaining org-level row fails the upgrade loudly.

## Money rules

1. `Charge.amount` → `paid` = Σ payments → `outstanding = amount − paid`, all derived read-time.
2. A payment that would exceed the outstanding amount → 422 `INVALID_INPUT`.
3. `method` is a free string (the method catalog is a client-side concern; the backend records what the
   frontend sends).

## Idempotency rules (PF4)

- Every mutation accepts `Idempotency-Key`; the claim is staged as the **first statement** of the
  transaction (before the permission check).
- Same key + same fingerprint → replay of the stored logical outcome (`Idempotent-Replay: true`), never a
  double mutation (no double stock movement, no double payment, no double booking).
- Same key + different payload → 409 `IDEMPOTENCY_KEY_REUSED`.
- Receipts are durable PostgreSQL rows (`command_receipts`) — no Redis, no outbox.