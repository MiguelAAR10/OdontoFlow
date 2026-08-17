# API Reference — OdontoFlow Backend

Curated reference for the HTTP surface at `HEAD`. The **authoritative** contract is the generated
`docs/api/openapi.yaml` / `openapi.json` (and live `/openapi.json` + `/docs` on a running server) — this
document organizes the same surface by domain for humans and agents, and notes the rules that matter.

- Base path: none (routes are domain-prefixed).
- Tenancy: every request runs in the acting organization resolved from the HTTP context (today: the
  trusted `system` principal in the bootstrap org — PF3). The tenant **never** comes from a query or body
  field.
- Errors: one stable envelope `{"error": {"code", "message", "details"}}`. Codes:
  `INVALID_INPUT` (422) · `NOT_FOUND` (404) · `ENTITY_INACTIVE` (409) · `CAPABILITY_MISSING` (409) ·
  `SLOT_BLOCKED` (409) · `APPOINTMENT_CONFLICT` (409) · `IDEMPOTENCY_KEY_REUSED` (409) ·
  `PERMISSION_DENIED` (403) · `NETWORK/UNKNOWN` (client-side).
- Idempotency: mutations accept the `Idempotency-Key` header; the same key + fingerprint replays the
  stored outcome with `Idempotent-Replay: true`; a reused key with a different payload → 409
  `IDEMPOTENCY_KEY_REUSED`.
- Monetary and stock quantities are decimal strings in responses (e.g. `"150.00"`), numbers in requests.

## Health

| Method & path | Permission | Purpose |
|---|---|---|
| `GET /health` | none | Liveness probe. |

## Catalog

| Method & path | Permission | Purpose |
|---|---|---|
| `GET /services` | `services.read` | List active services. |
| `POST /services` | `services.manage` | Create a service. Body: `{name, duration_minutes, is_active?}`. Duration is catalog-authoritative — clients never supply end time. |
| `GET /practitioners/eligible?service_id&location_id` | `practitioners.read` | Practitioners eligible (active capability) for a service at a location. |

## Commercial (leads)

| Method & path | Permission | Purpose |
|---|---|---|
| `GET /leads?search?commercial_status` | `leads.read` | List/search leads. |
| `POST /leads` | `leads.create` | Create a lead. Body: `{full_name, contact_phone?, contact_email?, acquisition_source, service_need_id?}`. At least one contact channel is required. |
| `GET /leads/{lead_id}` | `leads.read` | One lead. |

## Organization (locations & practitioners)

| Method & path | Permission | Purpose |
|---|---|---|
| `GET /locations` | `locations.read` | List locations (branches) of the org. |
| `POST /locations` | `locations.manage` | Create a location. Body: `{name, timezone}`. |
| `POST /practitioners` | `practitioners.manage` | Create a practitioner (global identity). Body: `{display_name, is_active?}`. |
| `POST /capabilities` | `capabilities.manage` | Grant a practitioner eligibility. Body: `{practitioner_id, service_id, location_id, is_active?}`. |

## Scheduling

| Method & path | Permission | Purpose |
|---|---|---|
| `POST /availability-rules` | `availability.manage` | Practitioner × location weekly rule. Body: `{practitioner_id, location_id, day_of_week (0–6), start_local, end_local}`. |
| `POST /schedule-blocks` | `availability.manage` | One-off blocked interval. Body: `{practitioner_id, location_id, start_utc, end_utc}`. |
| `POST /slots/query` | `appointments.read` | Bookable slots for a service/location in a window. Body: `{service_id, location_id, window_start, window_end}`. Pure 15-minute grid in the location timezone. |
| `GET /appointments?from_date&to_date&location_id&practitioner_id` | `appointments.read` | Agenda read; half-open `[from, to)` window (RFC 3339). |
| `GET /appointments/{id}` | `appointments.read` | One appointment (with names joined). |
| `POST /appointments` | `appointments.create` | Book + confirm. Body: `{lead_id, service_id, location_id, practitioner_id, start}` (RFC 3339). Validates lead/service/location/membership/capability + bookable slot; the state is always `confirmed`. |
| `POST /appointments/{id}/reschedule` | `appointments.reschedule` | Body: `{new_start}`. Confirmed-only; slot re-checked. |
| `POST /appointments/{id}/cancel` | `appointments.cancel` | Empty body. Confirmed-only; interval preserved. |

## Clinical

| Method & path | Permission | Purpose |
|---|---|---|
| `GET /patients?search` | `patients.read` | List/search patients. |
| `POST /patients` | `patients.create` | Create a patient. Body: `{full_name, dni?, sexo? (M/F/O), phone?, birth_date?}`; DNI `^\d{8}$`, unique per org. |
| `GET /patients/{id}` | `patients.read` | One patient. |
| `GET /visits?patient_id` | `visits.read` | List visits. |
| `POST /visits` | `visits.create` | Record attendance. Body: `{patient_id, appointment_id?} — XOR — {patient_id, practitioner_id, location_id}` (walk-in). With an appointment, practitioner/location are **derived** from it; the appointment must be `confirmed`. |
| `GET /visits/{id}` | `visits.read` | Visit detail with executions. |
| `GET /visits/{id}/executions` | `executions.read` | Executions of a visit. |
| `POST /visits/{id}/executions` | `executions.create` | Record one executed service. Body: `{service_id, executed_price}`. |

## Economics

| Method & path | Permission | Purpose |
|---|---|---|
| `POST /executions/{id}/charges` | `charges.create` | Charge an execution. Body: `{amount?}` (defaults to the executed price). |
| `GET /charges?execution_id` | `charges.read` | The charge list — the cash-visible economic state: `{id, service_execution_id, amount, paid, outstanding, created_at}`. |
| `GET /charges/{id}` | `charges.read` | One charge (paid/outstanding are derived read-time). |
| `GET /charges/{id}/payments` | `payments.read` | Payment history of a charge. |
| `POST /charges/{id}/payments` | `payments.create` | Record a payment. Body: `{amount, method}` (method is a free string). Overpayment → 422 `INVALID_INPUT`. |
| `POST /executions/{id}/consumptions` | `consumptions.create` | Clinical consumption. Body: `{product_id, quantity, unit_price}`. Emits the SALIDA movement **at the execution's visit location** and records the 1:1 causal link. |
| `GET /executions/{id}/consumptions` | `consumptions.read` | Consumptions of an execution (with product names). |

## Inventory (location-aware)

| Method & path | Permission | Purpose |
|---|---|---|
| `GET /products?search&kind` | `products.read` | List products (`consumible` / `reventa`). A product is **not** stock. |
| `POST /products` | `products.create` | Create a product. Body: `{name, unit, kind}`. `extra=forbid` — no category/minimum/supplier fields exist. |
| `GET /products/{id}` | `products.read` | One product. |
| `POST /products/{id}/entries` | `movements.create` | Stock entry at a location. Body: `{location_id, quantity (>0), unit_price?}`. |
| `POST /products/{id}/adjustments` | `movements.create` | Reason-required signed correction. Body: `{location_id, quantity (≠0), reason}`. Negative requires enough stock. |
| `GET /products/{id}/movements?location_id` | `movements.read` | Kardex of one product × location. Movement vocabulary: `ENTRADA`, `SALIDA`, `ADJUSTMENT`, `TRANSFER_OUT`, `TRANSFER_IN`. |
| `GET /products/{id}/balance?location_id` | `movements.read` | Derived read-time balance `{product_id, location_id, available}`. Never stored. |
| `POST /products/{id}/transfers` | `movements.create` | Atomic location transfer. Body: `{origin_location_id, destination_location_id, quantity (>0), reason?}`. One transaction writes TRANSFER_OUT + TRANSFER_IN with a shared server-generated `transfer_id`. |

## Conventions that keep this surface honest

1. **Routers are thin**: HTTP shape → Pydantic schema → application service → typed response. No business
   logic in routers.
2. **`extra="forbid"` on mutation bodies**: clients cannot smuggle state (duration, end time, stock,
   category, location on consumption) into the domain.
3. **Everything stateful is a mutation with audit + idempotency**: creations return 201; reads 200;
   replay returns the stored outcome.
4. **No endpoint invents data**: balance is derived, paid/outstanding are derived, eligibility is derived
   from capabilities, availability is pure logic over rules/blocks/appointments.