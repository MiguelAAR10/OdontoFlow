# B0.5 — Huecos de dominio (spec)

- **Estado:** aprobado por coord-be (incluye decisión del coordinador: reversa solo humana, §2 y riesgo 7). **Tarjeta:** B0.5 (ERP-AGENTICO-01). **Depende de:** B0.
- **Fuentes:** plan §5 B0.5; SubCard B0.5; deep research §5 (`domain_events`, "nunca por agente");
  scout `.erp-reports/B0.5-inspector-C.md` (HEAD 4bcd873, alembic head 0019).
- **Invariantes que se mantienen:** PostgreSQL decide; servicios con `session.begin()` antes de leer;
  audit + evento atómicos con el cambio; FKs compuestas a `UNIQUE(organization_id, id)`; GiST
  practitioner-global intacto; envelope de error estable; permisos por código.

## 1. Citas: `completed` y `no_show`

- `ck_appointments_state` pasa a `state IN ('confirmed','cancelled','completed','no_show')`.
  `excl_appointments_confirmed_no_overlap` **no se toca** (sigue `WHERE state='confirmed'`).
- Transiciones permitidas (y solo estas): `confirmed → completed`, `confirmed → no_show`. Ambas exigen
  `start_utc <= now()` (la cita ya empezó). `now` = `datetime.now(UTC)` del servicio (mismo reloj que
  `booking.py`).
- Servicios en `app/scheduling/service.py`, con el mismo patrón que `cancel_appointment`:
  `complete_appointment(session, appointment_id, *, ctx=None, organization_id=None, idempotency=None)` y
  `mark_no_show(...)`. Pasos: `with session.begin()` → `claim_receipt` → `_lock_appointment` (FOR UPDATE,
  tenant-scoped) → `require_permission(APPOINTMENTS_RECORD_OUTCOME, location_id=appointment.location_id)` →
  `_require_confirmed` → comprobar `start_utc <= now` → cambiar `state` → `record_event`
  (`appointment.completed` / `appointment.no_show`, before/after = `_appointment_state`) →
  `record_domain_event` → `settle_receipt`.
  Orden claim → lock → permiso, copiado exacto de `cancel_appointment` (el permiso es por sede y necesita
  la cita cargada). Sin guard de tipo de principal: `complete`/`no_show` son por permiso.
- Errores: estado ≠ confirmed ⇒ `ENTITY_INACTIVE` (reusa `_require_confirmed`); cita futura ⇒
  `INVALID_INPUT` "The appointment has not started yet."; no existe u otra org ⇒ `NOT_FOUND`.
- Idempotencia: misma `Idempotency-Key` + mismos parámetros ⇒ replay (`Idempotent-Replay: true`, mismo
  cuerpo); otra key sobre una cita ya `completed` ⇒ `ENTITY_INACTIVE`.
- **Clínica sin cambios:** `create_visit` sigue exigiendo `state == 'confirmed'`. El flujo es:
  confirmed → crear visita (desde la cita) → `complete`. Una cita `completed` o `no_show` ya no puede
  originar visita (correcto para no_show; para completed obliga a crear la visita **antes**). Se
  documenta en el docstring de `complete_appointment` y aquí (`app/clinical` queda fuera de superficie).
- `cancel`/`reschedule` siguen rechazando todo lo que no sea `confirmed` (ya lo hace `_require_confirmed`).
- Emisión de `appointment.cancelled`: en `cancel_appointment` (scheduling) y en la cancelación confirmada
  por contacto `app/agent_tools/reception.py:824` (desviación autorizada, solo añadir la llamada).

## 2. Reversa de pago (registro nuevo, `payments` intacta)

- Tabla `payment_reversals`: `id` Identity PK, `organization_id` NOT NULL FK→organizations,
  `payment_id` NOT NULL, `reason` `String(500)` NOT NULL con `CHECK (length(btrim(reason)) > 0)`,
  `reversed_at timestamptz NOT NULL DEFAULT now()`, `created_by_principal_id` NOT NULL FK→principals.
  Constraints: `UNIQUE(organization_id, id)` (`uq_payment_reversals_organization_id`),
  `UNIQUE(organization_id, payment_id)` (`uq_payment_reversals_org_payment` ⇒ una reversa **total** por
  pago), FK compuesta `fk_payment_reversals_organization_payment` (org, payment_id) →
  `payments(organization_id, id)` RESTRICT.
  No hay columna `amount`: la reversa siempre es del monto completo del pago.
- **Saldo pagado** = `SUM(payments.amount) − SUM(amount de pagos revertidos)`, implementado como
  `SUM(payments.amount) WHERE NOT EXISTS reversal` (equivalente y sin doble join). Un solo predicado
  `_payment_not_reversed()` (en `app/economics/service.py`) lo comparten: `charge_paid_amount` (lo usa
  directamente, y con él el chequeo de sobrepago de `create_payment`) y `_net_paid_expr()` (sin argumentos,
  subconsulta correlacionada a `Charge`), que usan `_charge_projection_rows` (filtros unpaid/partial/paid) y
  `_follow_up_projection_rows` (`outstanding`).
- Servicio `reverse_payment(session, payment_id, data: PaymentReverse, *, ctx=None, organization_id=None,
  idempotency=None)` en `app/economics/service.py`: `_require_human_principal(resolved)` **primero**, antes de
  `session.begin()` (sin receipt ni lectura). Es una allow-list (plan §2 principio 5: las reversas son L4):
  `principal_type != 'human'` ⇒ `INVALID_INPUT` "Only an authenticated human principal may reverse a
  payment." — agent, integration y system reciben el mismo error. Mismo estilo que
  `_require_human_reviewer` (`app/scheduling/router.py`); economics no importa `app/agent_tools`.
  → `with session.begin()` → `claim_receipt` → `require_permission(PAYMENTS_REVERSE)` →
  lock del **charge** del pago `FOR UPDATE` (serializa con `create_payment`) → si ya existe reversa ⇒
  `INVALID_INPUT` "The payment is already reversed." (la UNIQUE es la autoridad final ante carrera;
  `IntegrityError` → mismo `AppError` tras rollback) → insertar fila → `record_event`
  (`payment.reversed`) → `record_domain_event` → `settle_receipt`.
- Se puede revertir un pago `unverified` o `verified`; `verification_status` no cambia.
- El charge vuelve a `partial`/`unpaid` por derivación; **no** se reabre ningún `charge_follow_up` ya
  cerrado por pago total (B4 detecta el saldo vencido por SQL).
- `PaymentRead` añade `reversed: bool` y `reversed_at: datetime | None` (aditivo). `Payment.reversed_at` es un
  `column_property` (subconsulta correlacionada sobre `uq_payment_reversals_org_payment`) que se carga en el
  mismo SELECT que cualquier `Payment`; no hay atributos ad-hoc en las instancias.
- `payment.recorded` se emite en `create_payment`.

## 3. Puntos de reorden

- Tabla `reorder_points`: `id` Identity PK, `organization_id`, `product_id`, `location_id`,
  `min_quantity Numeric(10,2) NOT NULL CHECK (min_quantity >= 0)`, `updated_at timestamptz NOT NULL DEFAULT
  now()`. `UNIQUE(organization_id, id)`, `UNIQUE(organization_id, product_id, location_id)`
  (`uq_reorder_points_org_product_location`), FKs compuestas (org, product_id) → `products(organization_id,
  id)` y (org, location_id) → `locations(organization_id, id)`, RESTRICT (como el ledger).
- `upsert_reorder_point` (`app/inventory/service.py`): `INSERT … ON CONFLICT (organization_id, product_id,
  location_id) DO UPDATE SET min_quantity, updated_at` dentro de `session.begin()`, con `record_event`
  (`reorder_point.set`, before/after). Idempotente por naturaleza (PUT) y además acepta `Idempotency-Key`.
  Producto y sede se validan tenant-scoped (`NOT_FOUND` / `ENTITY_INACTIVE`).
- `list_low_stock(session, *, location_id=None, ctx)`: filas con `available_balance < min_quantity`,
  una consulta SQL (balance con la misma suma firmada que `available_balance`, agregada con `GROUP BY` y
  `LEFT JOIN` al ledger). Orden: `location_id, product_id`.
- **Evento `inventory.below_reorder` solo en el cruce:** en los caminos que restan stock —
  `register_adjustment` con cantidad negativa, `transfer_product` (lado origen) y
  `create_service_consumption` (SALIDA, `app/economics/service.py:460`) — con el producto ya bloqueado,
  se calcula `before = available_balance` antes del movimiento y `after` después; se emite solo si existe
  punto de reorden y `before >= min > after`. Un movimiento que parte de `before < min` no emite. Poner o
  cambiar el mínimo **no** emite (no es un movimiento). Payload: `product_id, location_id, min_quantity,
  balance_before, balance_after, movement_id`.

## 4. Waitlist mínima (`app/scheduling/waitlist.py`)

- Tabla `waitlist_entries`: `id` Identity PK, `organization_id`, `lead_id` NOT NULL, `patient_id` NULL,
  `service_id` NOT NULL, `location_id` NULL, `practitioner_id` NULL, `earliest_date date NOT NULL`,
  `latest_date date NOT NULL`, `preferred_window String(10) NOT NULL DEFAULT 'any'`, `status String(10) NOT NULL
  DEFAULT 'open'`, `notes String(500) NULL`, `created_at timestamptz NOT NULL DEFAULT now()`.
  CHECKs: `ck_waitlist_entries_dates (latest_date >= earliest_date)`,
  `ck_waitlist_entries_window IN ('any','morning','afternoon')`,
  `ck_waitlist_entries_status IN ('open','offered','booked','cancelled','expired')`.
  `UNIQUE(organization_id, id)`. FKs compuestas RESTRICT: (org, lead_id)→leads, (org, patient_id)→patients,
  (org, service_id)→services, (org, location_id)→locations, (org, practitioner_id)→
  `practitioner_memberships(organization_id, practitioner_id)` (igual que appointments). Índice
  `ix_waitlist_entries_org_status_service (organization_id, status, service_id)`.
- Los estados `offered/booked/expired` y los filtros `location_id`/`service_id` no tienen productor en B0.5:
  quedan reservados para B4 (Backfill), que los necesita para ofrecer huecos por sede/servicio.
- Servicios: `create_waitlist_entry` (valida referencias activas tenant-scoped; `waitlist.created`
  audit + domain_event), `list_waitlist_entries(status, location_id, service_id)`,
  `cancel_waitlist_entry` (solo desde `open|offered` ⇒ `cancelled`; otro estado ⇒ `ENTITY_INACTIVE`;
  FOR UPDATE). `offered/booked/expired` no tienen transición aquí (B4).
- Router nuevo en `app/scheduling/waitlist.py` montado en `app/__init__.py` con
  `require_authenticated_context`.

## 5. `domain_events` (`app/events/`)

- `app/events/models.py` `DomainEvent` según deep research §5: `id` Identity PK, `organization_id` NOT NULL
  FK→organizations RESTRICT, `event_type Text`, `aggregate_type Text`, `aggregate_id Text`,
  `payload JSONB NOT NULL`, `occurred_at timestamptz NOT NULL DEFAULT now()`, `correlation_id Text NULL`.
  Además `UNIQUE(organization_id, id)` e índice `ix_domain_events_org_type_id (organization_id, event_type, id)`.
- `app/events/service.py`: `record_domain_event(session, *, ctx, event_type, aggregate_type, aggregate_id,
  payload) -> DomainEvent`: `session.add` + `flush`, **nunca** commit ni `begin`; toma `organization_id` y
  `correlation_id` de `ctx`. Constantes de `event_type` en `app/events/types.py`.
- Emisores (siempre dentro de la transacción del cambio, justo después de `record_event`):
  `appointment.cancelled`, `appointment.completed`, `appointment.no_show`, `payment.recorded`,
  `payment.reversed`, `inventory.below_reorder`, `waitlist.created`. `charge.overdue` no es evento.
- No hay API de lectura en B0.5 (la consumen B3/B4).

## 6. Migración `0020_domain_gaps.py` (down_revision `0019`)

Upgrade: (1) drop + create `ck_appointments_state`; (2) `domain_events`, `payment_reversals`,
`reorder_points`, `waitlist_entries` con sus constraints/índices; (3) permisos por `bulk_insert` +
`INSERT INTO role_permissions … r.code='system' AND p.code = ANY(:codes)` (patrón 0018).
Downgrade simétrico en orden inverso: borra role_permissions/permissions de los 5 códigos, drop de las 4
tablas, y restaura `ck_appointments_state` al CHECK original. Si existen filas `completed`/`no_show`, el
downgrade **falla limpio** antes de tocar nada: `DO $$ … RAISE EXCEPTION 'downgrade 0020: hay citas
completed/no_show' …`, sin reescribir datos. Igual si `payment_reversals` tiene filas (`RAISE EXCEPTION
'downgrade 0020: hay filas en payment_reversals …'`): borrarla devolvería en silencio los pagos revertidos a
cobros pagados (cambio de estado de dinero). Sin backfill (todas las columnas son de tablas nuevas).

Permisos nuevos (48 → 53): `appointments.record_outcome`, `payments.reverse`, `reorder_points.manage`,
`waitlist.read`, `waitlist.manage` (constantes en `app/iam/permissions.py` y en `PERMISSION_CODES`).
El rol `system` recibe los 5 (patrón 0018); el grant de `payments.reverse` a `system` es inerte porque el guard
human-only lo rechaza antes del permiso. Perfil `reception-staff-demo` (credencial `integration`) recibe 4, todos
menos `payments.reverse` (irá al perfil humano `administrador` en la tarjeta IDN).

## 7. Contrato HTTP

Todas bajo `require_authenticated_context`, envelope estándar, `response_model` declarado. Las mutaciones
aceptan `Idempotency-Key` (UUIDv4; obligatorio para principals `agent`/`integration`, como hoy).
401 sin credencial, 403 `PERMISSION_DENIED` sin permiso, 409 `IDEMPOTENCY_KEY_REUSED` si la key se reusa
con otros parámetros, 422 `INVALID_INPUT` en validación de schema.

| Método y ruta | Request | Response | Permiso | Errores específicos |
|---|---|---|---|---|
| POST `/appointments/{id}/complete` | vacío (`extra="forbid"`) | 200 `AppointmentRead` | `appointments.record_outcome` | 404 NOT_FOUND; 409 ENTITY_INACTIVE (≠confirmed); 422 INVALID_INPUT (futura) |
| POST `/appointments/{id}/no-show` | vacío | 200 `AppointmentRead` | `appointments.record_outcome` | ídem |
| POST `/payments/{id}/reverse` | `PaymentReverse{reason: str 1..500}` | 200 `PaymentRead` (`reversed=true`) | `payments.reverse` | 404; 422 INVALID_INPUT (ya revertido; principal no humano: agent/integration/system) |
| PUT `/products/{id}/reorder-points/{location_id}` | `ReorderPointUpsert{min_quantity: Decimal >= 0}` | 200 `ReorderPointRead{id, product_id, location_id, min_quantity, updated_at}` | `reorder_points.manage` | 404 producto/sede; 409 ENTITY_INACTIVE |
| GET `/inventory/low-stock?location_id=` | — | 200 `list[LowStockRead{product_id, product_name, unit, location_id, location_name, balance, min_quantity}]` | `products.read` + `movements.read` | 404 sede |
| POST `/waitlist` | `WaitlistEntryCreate{lead_id, patient_id?, service_id, location_id?, practitioner_id?, earliest_date, latest_date, preferred_window='any', notes?}` | 201 `WaitlistEntryRead` (todos los campos + `status`, `created_at`) | `waitlist.manage` | 404/409 ENTITY_INACTIVE en referencias; 422 INVALID_INPUT (fechas) |
| GET `/waitlist?status=&location_id=&service_id=` | — | 200 `list[WaitlistEntryRead]` (orden `created_at, id`) | `waitlist.read` | — |
| POST `/waitlist/{id}/cancel` | vacío | 200 `WaitlistEntryRead` | `waitlist.manage` | 404; 409 ENTITY_INACTIVE |

Los códigos HTTP salen del mapeo actual de `app/errors.py` (no se toca). Regenerar `docs/api/openapi.*`.

## 8. Pruebas (TDD, PostgreSQL real) — `tests/test_domain_gaps*.py`

- Citas: pasada confirmed → complete (200, audit + domain_event); → no_show; futura → no_show ⇒ INVALID_INPUT;
  cancelled → complete ⇒ ENTITY_INACTIVE; doble complete misma key ⇒ replay, una fila de evento; otra key
  ⇒ ENTITY_INACTIVE; cita completed libera el GiST (se puede reservar otra encima si hay slot); otra org ⇒ 404;
  sin permiso ⇒ 403; visita desde completed ⇒ ENTITY_INACTIVE (documenta orden).
- Reversa: charge paid → reverse ⇒ partial/unpaid en `GET /charges?status=` y en follow-ups outstanding;
  nuevo pago tras reversa permitido hasta el nuevo saldo; doble reversa ⇒ INVALID_INPUT; carrera de dos
  reversas (dos sesiones + Barrier) ⇒ una sola fila; principal no humano (agent e integration con
  `payments.reverse`, y system vía `default_context`) ⇒ mismo INVALID_INPUT y sin fila, receipt ni domain_event;
  el perfil `reception-staff-demo` no contiene `payments.reverse`.
- Reorden: PUT dos veces = una fila; low-stock lista solo `balance < min`; ajuste/transfer/consumo que cruza
  ⇒ un evento; segundo movimiento ya por debajo ⇒ ninguno; tenant isolation en PUT y GET.
- Waitlist: create/list/filtros/cancel; `latest < earliest` ⇒ 422; lead de otra org ⇒ 404 y además
  inserción directa cross-tenant rechazada por FK; cancel de cancelled ⇒ ENTITY_INACTIVE.
- domain_events: presente tras commit; ausente si el servicio falla después de emitir (se fuerza fallo
  posterior en la misma transacción) y tras `rollback` explícito.
- Migración: upgrade/downgrade/upgrade en BD desechable (`_temporary_database_url` de
  `tests/test_migrations.py`); downgrade con fila `no_show` falla con mensaje claro; downgrade con una fila en `payment_reversals` también
  falla y deja la BD en 0020. Bump `HEAD_REVISION`
  a "0020", `EXPECTED_TABLES` +4, `test_authorization.py` 48 → 53.

## 9. Seed (`scripts/seed_demo.py`)

3 entradas de waitlist `open` (leads existentes, "Limpieza dental", sedes distintas); `reorder_points` que
dejan `Anestesia lidocaína 2%` bajo en Lince (min 10, balance 4) y holgado en Jesús María (min 10, 120).
Idempotente por clave natural (lead+service+status open; product+location).

## 10. Riesgos / Preguntas

1. **"VALIDATION" no existe** en `ErrorCode` (hay `INVALID_INPUT`, `NOT_FOUND`, `ENTITY_INACTIVE`, …). La spec
   usa `INVALID_INPUT` para "aún no empezó" y `ENTITY_INACTIVE` para estado ≠ confirmed.
2. `create_visit` exige `confirmed`: si recepción marca `completed` antes de crear la visita, no hay manera
   de crear la visita después. Se acepta según la decisión 1; B2/B4 deben ordenar la UI.
3. `completed`/`no_show` salen del GiST: el hueco queda libre. Como la cita ya empezó, solo importa si
   alguien reserva en el pasado (hoy `book_appointment` no lo impide). No se corrige aquí.
4. `reschedule_appointment` (scheduling y agent_tools) **no** emite `appointment.cancelled`; si B4 lo
   necesita para ofrecer huecos, habría que añadir `appointment.rescheduled` (fuera de alcance).
5. Consumos (`create_service_consumption`, economics) generan SALIDA: la spec emite ahí `below_reorder`
   (está en superficie `app/economics`). Ojo con importar `app.events` desde economics e inventory (sin ciclos).
6. `record_domain_event` hace `flush`: añade un INSERT por cambio; no hay outbox reader aún (B4).
7. **Decidido (coordinador, plan §2 principio 5 — las reversas son L4):** la reversa es solo para principals
   `human` (allow-list). `integration`, `agent` y `system` quedan fuera aunque tengan el permiso. Consecuencia:
   la credencial staff demo (`integration`) ya no puede revertir hasta que IDN añada el perfil humano
   `administrador`.
8. `tests/test_migrations.py::test_downgrade_*` usa la BD compartida; el nuevo test de downgrade con datos va
   en BD desechable para no romperla.

## 11. Fuera de alcance

Transiciones `offered/booked/expired` de waitlist y su oferta al paciente (B4); `charge.overdue` como evento
(SQL en B4); lector/consumidor de `domain_events`, `agent_jobs`, `agent_proposals` (B2/B4); reversas
parciales; reapertura de follow-ups; `appointment.rescheduled`; cambios en `create_visit`,
`availability.py`, `app/errors.py`, `app/audit`, GiST; UI; notificaciones al paciente.
