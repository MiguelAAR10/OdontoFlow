# Flows — OdontoFlow Backend

The deterministic journeys the platform guarantees, end to end. Each flow lists the service boundary
rule that makes it safe. The pilot E2E (frontend repo: `test/pilot-e2e.test.ts`) proves the full
clinical → economic → inventory journey over real HTTP + PostgreSQL with no mocks.

## 1. Booking flow (Lead → confirmed Appointment)

```
Lead exists? ── Service active? ── Location active? ── practitioner member + capability?
   → availability inputs (rules + blocks + appointments, half-open)
   → generate_slots (pure, 15-min grid, location timezone)
   → requested [start, end) must be a bookable slot
   → insert Appointment (state=confirmed)
   → audit + settle idempotency receipt   (one transaction)
```

- End is derived from `Service.duration_minutes`; clients never send it.
- Two concurrent bookings for the same practitioner interval: the partial GiST exclusion constraint
  settles the race — one commits, the other gets `APPOINTMENT_CONFLICT` (23P01 → 409).
- Reschedule/cancel: confirmed-only (`_require_confirmed`), row locked `FOR UPDATE` first, interval
  preserved on cancel.

## 2. Clinical flow (Patient → Visit → ServiceExecution)

```
POST /patients {full_name, dni?…}          → Patient (DNI unique per org)
POST /visits {patient_id, appointment_id}  → appointment must be confirmed;
                                             practitioner/location DERIVED from it
                                             (XOR walk-in: practitioner+location required)
POST /visits/{id}/executions {service_id, executed_price} → ServiceExecution
```

- The attendance instant is domain-owned (server default), never client-supplied.
- Every step is permission-checked, idempotent and audited.

## 3. Clinical consumption → stock (ServiceConsumption → SALIDA)

```
POST /executions/{id}/consumptions {product_id, quantity, unit_price}
  → resolve execution → its Visit.location_id   (location NEVER client-supplied)
  → product-row FOR UPDATE at that location
  → floor check: available ≥ quantity, else 422 INVALID_INPUT
  → insert ServiceConsumption
  → insert SALIDA movement with location_id = visit location,
    id_consumo_origen = consumption id (1:1, unique)
  → audit + receipt         (one transaction)
```

- Balances of **other locations are untouched** (proven by test).
- Concurrent consumptions cannot overspend: the product-row lock serializes the floor check.

## 4. Economic flow (Charge → Payment)

```
POST /executions/{id}/charges {amount?}    → Charge (default: executed_price)
POST /charges/{id}/payments {amount, method} + Idempotency-Key
  → charge FOR UPDATE → paid = Σ payments → outstanding = amount − paid
  → amount > outstanding ? 422 INVALID_INPUT
  → insert Payment → audit + receipt
```

- `GET /charges` is the cash-visible economic state: `amount`, `paid`, `outstanding`, `created_at`
  (payments joinable via `/charges/{id}/payments`). The Cash UI derives 'Por cobrar' = Σ outstanding.
- Partial → full payments: outstanding reaches 0; overpayment is rejected by the backend, never
  hidden by client math.

## 5. Inventory flows (entry / adjustment / transfer)

```
Entry:      POST /products/{id}/entries {location_id, quantity>0, unit_price?} → ENTRADA
Adjustment: POST /products/{id}/adjustments {location_id, quantity≠0, reason}  → ADJUSTMENT
            (negative requires stock at that location)
Transfer:   POST /products/{id}/transfers {origin, destination, quantity>0, reason?}
            → one transaction:
                TRANSFER_OUT (origin) + TRANSFER_IN (destination),
                shared server-generated transfer_id
              trigger (DEFERRABLE, at COMMIT):
                pair shares org/product/quantity, distinct locations,
                exactly one OUT + one IN per transfer_id,
                transfer_id only on TRANSFER rows
            → insufficient origin stock rejected (FOR UPDATE floor)
            → audit + receipt
```

- Conservation: `Δorigin = −q`, `Δdestination = +q`, total unchanged (proven).
- Idempotent: a replay of the same key returns the stored outcome; stock moves exactly once.
- Reversal of a transfer is another transfer, never an edit.

## 6. Error flow (one envelope)

```
any failure ──► AppError {code, message, details}         → JSON {"error": {...}}
Pydantic validation failure                              → 422 INVALID_INPUT (details hidden by design)
IntegrityError 23P01 (GiST exclusion / receipt conflict)  → 409 APPOINTMENT_CONFLICT or receipt replay
other IntegrityError                                      → 409/500 generic (no SQLSTATE leak)
```

Frontends map the envelope once (`toApiError`) and render `message`; they never parse SQL or stack
traces.

## 7. Idempotency flow (PF4)

```
request with Idempotency-Key
  → claim staged FIRST (org, operation, key, fingerprint)
  → execute mutation in the same transaction
  → settle receipt with the logical outcome   (commits atomically)
replay (same key + fingerprint) → stored outcome returned, Idempotent-Replay: true
reuse (same key, different fingerprint)       → 409 IDEMPOTENCY_KEY_REUSED
```

Applies to: booking, reschedule, cancel, patients, visits, executions, consumptions, charges, payments,
product creation, entries, adjustments, transfers.