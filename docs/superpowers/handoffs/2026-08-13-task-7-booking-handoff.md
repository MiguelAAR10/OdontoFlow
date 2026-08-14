# Task 7 — Booking Transaction (handoff)

**Fecha:** 2026-08-13 · **Baseline SHA:** `a116109` (61 tests PASS) · **Estado:** completo, sin commitear.

---

## 1. Objetivo

Implementar el caso de uso autoritativo `book_appointment(session, ...)`: confirmar una cita en UNA transacción,
con revalidación in-transaction, duración tomada del catálogo, verificación de disponibilidad vía el motor puro de
Task 6, un `AuditEvent` en la misma transacción, y la exclusión GiST como autoridad final de concurrencia.

## 2. Archivos escritos

| Archivo | Estado |
|---|---|
| `app/scheduling/service.py` | **nuevo** — `book_appointment` |
| `app/audit/service.py` | **nuevo** — `record_event` (append-only, no commitea) |
| `tests/test_booking.py` | **nuevo** — 28 tests de integración PostgreSQL |
| `docs/superpowers/handoffs/2026-08-13-task-7-booking-handoff.md` | **nuevo** — este documento |

No se modificó ningún archivo existente (`git status` muestra sólo untracked). Migración `0001`, modelos,
`app/errors.py`, `app/db.py`, `app/scheduling/availability.py`, contratos de commercial/catalog/organization y
`tests/conftest.py` quedaron intactos. Sin commits.

## 3. Contrato `book_appointment`

```python
book_appointment(
    session: Session,
    *,
    lead_id: int,
    service_id: int,
    location_id: int,
    practitioner_id: int,
    start: datetime,            # timezone-aware, obligatorio
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> Appointment
```

Todos los parámetros son keyword-only. **No existe** parámetro `duration_minutes` ni `end`: pasarlos levanta
`TypeError` (probado). El llamador elige *quién* y *cuándo*; OdontoFlow decide duración, capacidad, disponibilidad
y conflicto.

## 4. Propiedad de la transacción

`book_appointment` **abre la transacción antes de la primera lectura de booking** (`with session.begin():`),
evitando la trampa de autobegin (consultar primero y luego intentar `begin()`). La única operación previa al
`begin()` es la validación de tipo/tz de `start`, que no toca la base.

Consecuencia contractual: el caso de uso es dueño de la transacción y debe recibir una `Session` ociosa. Si ya hay
una transacción abierta, SQLAlchemy falla de forma explícita (`InvalidRequestError`) en lugar de unirse en silencio
a un ámbito ajeno. En FastAPI esto se cumple naturalmente con la dependencia `get_db` por request.

Salida: al cerrar el `with`, `Appointment` + `AuditEvent` se commitean juntos. Ante cualquier excepción el bloque
hace rollback completo: no queda Appointment parcial ni AuditEvent huérfano (probado en ambos sentidos).

## 5. Revalidación autoritativa in-transaction

Orden y códigos (todos `AppError` de `app/errors.py`, sin mapeo HTTP en el servicio):

| Verificación | Error |
|---|---|
| `Lead` existe | `NOT_FOUND` |
| `Service` existe / activo | `NOT_FOUND` / `ENTITY_INACTIVE` |
| `Location` existe / activa | `NOT_FOUND` / `ENTITY_INACTIVE` |
| `Practitioner` existe / activo | `NOT_FOUND` / `ENTITY_INACTIVE` |
| `PractitionerCapability(practitioner, service, location)` presente **y** activa | `CAPABILITY_MISSING` |
| Intervalo pedido es slot reservable | `SLOT_BLOCKED` |
| `start` naive o no-datetime | `INVALID_INPUT` |

Capability ausente y capability inactiva devuelven el **mismo** `CAPABILITY_MISSING`: hacia afuera son el mismo
hecho (ese practitioner no puede prestar ese servicio en esa sede) y distinguirlos filtraría estructura interna.

## 6. Uso del motor de Task 6

El servicio no reimplementa reglas de intervalos: adapta filas ORM y delega en `generate_slots`.

- `AvailabilityRule` (practitioner + location, todas las reglas recurrentes).
- `ScheduleBlock` (practitioner + location) solapados con `[start, end)`.
- `Appointment` confirmados **del practitioner, sin filtrar por sede**, solapados con `[start, end)`.
  El GiST es practitioner-wide; si el preflight filtrara por sede, una doble reserva entre sedes llegaría a la base
  como conflicto crudo en vez de un `SLOT_BLOCKED` limpio.

Llamada: `generate_slots(rules, blocks, appointments, duration_minutes, start_utc, end_utc, location.timezone)`.
La ventana es exactamente el intervalo pedido, de modo que el único candidato posible es `(start_utc, end_utc)`;
si esa tupla no está en el resultado → `SLOT_BLOCKED`. Esto cubre en un solo predicado: fuera de disponibilidad,
fuera de la grilla de 15 min, intervalo que se pasa del fin de la ventana, bloqueo y colisión con cita confirmada.
Las citas `cancelled` no bloquean (las filtra el propio motor).

## 7. Duración, tiempo y UTC

- La duración sale **siempre** de `services.duration_minutes` leído dentro de la transacción; `end = start + duration`.
- `start` debe ser timezone-aware; naive → `INVALID_INPUT` (nunca se asume una zona).
- Se normaliza con `astimezone(UTC)` y se persiste en UTC; dos instantes equivalentes en zonas distintas producen
  el mismo intervalo (probado).
- La disponibilidad se evalúa con `location.timezone` (IANA), no con la zona del llamador.

## 8. Frontera de errores: 23P01 no se traduce aquí

El servicio **no** captura `IntegrityError`. Cuando la exclusión GiST rechaza el insert, el error sube con
SQLSTATE `23P01` intacto y el handler de Task 3 lo convierte en `409 APPOINTMENT_CONFLICT`. Probado de forma
determinista en `test_booking_defeated_by_a_committed_row_propagates_sqlstate_23P01`.

Preflight y GiST son capas distintas y no intercambiables: el preflight da errores claros sobre estado conocible;
el GiST es la autoridad final bajo concurrencia.

## 9. Auditoría atómica

`app/audit/service.py::record_event` sólo hace `session.add` — nunca commitea ni abre transacción — para que la
atomicidad la decida el caso de uso. En un booking exitoso escribe exactamente una fila:

- `entity_type = "appointment"`, `action = "appointment.created"`, `entity_id = str(appointment.id)`
- `after_state = {"id", "start_utc", "end_utc", "state"}` (instantes en ISO-8601 UTC), `before_state = None`
- `actor_id`/`actor_type`: los provistos, o `"system"`/`"system"` por defecto (ambas columnas son NOT NULL)
- `correlation_id`: sólo si el borde de la petición lo entrega
- `occurred_at`: `server_default now()`

`session.flush()` asigna el id antes de construir el payload, de modo que el audit referencia la cita real.

## 10. Diseño del test de concurrencia (y hallazgo)

**Por qué dos hilos + barrera no bastan.** Se midió: con `threading.Barrier(2)` ambos hilos arrancan con 0.3 ms de
diferencia, pero la fase de lectura del ORM es CPU-Python y retiene el GIL, así que un hilo completó su transacción
entera (12 ms) mientras el otro casi no avanzó; el perdedor fallaba en el **preflight** (`SLOT_BLOCKED`) sin llegar
nunca al constraint. Una aserción estricta sobre 23P01 con sólo la barrera es flaky.

**Solución (sin hooks en producción, sin `sleep()`).** El hilo principal toma `LOCK TABLE appointments IN EXCLUSIVE
MODE`: los `SELECT` planos (ACCESS SHARE) siguen pasando, así que **ambos hilos completan su preflight y ven el slot
libre**, mientras ambos `INSERT` (ROW EXCLUSIVE) quedan bloqueados. El principal espera sondeando `pg_locks` hasta
ver 2 inserts bloqueados (poll acotado por `monotonic()`, sin `sleep`), y recién ahí libera. Desde ese punto decide
sólo el GiST. Esto fija justamente el entrelazado peligroso que hay que probar.

**Hallazgo real (no artefacto de test):** cuando ambos INSERT se liberan a la vez, cada transacción inserta su
entrada de índice y luego espera a la otra *mientras verifica la exclusión*, así que PostgreSQL puede resolver la
carrera por **detección de deadlock (`40P01`, `OperationalError`)** en vez de violación de exclusión (`23P01`).
Reproducido en ~3 de 25 corridas. En ambos casos el invariante se mantiene: sobrevive exactamente una cita
confirmada y exactamente un audit event.

Cobertura resultante, en dos tests complementarios:

1. `test_concurrent_bookings_of_the_same_slot_persist_exactly_one` — carrera real (2 Sessions independientes,
   2 hilos, `Barrier(2)`, sin `sleep`): exactamente uno commitea; el perdedor falla con conflicto de base
   (`23P01` **o** `40P01`); queda 1 cita confirmada y 1 audit event.
2. `test_booking_defeated_by_a_committed_row_propagates_sqlstate_23P01` — determinista: el hilo que reserva queda
   detenido en su INSERT *después* de un preflight que vio el slot libre, y recién entonces se commitea la fila
   ganadora debajo; su INSERT encuentra una entrada ya committeada → `IntegrityError` `23P01` inmediato (sin espera,
   sin deadlock), propagado sin traducir, sin cita ni audit huérfanos.
3. `test_session_is_reusable_after_exclusion_conflict_rollback` — tras el rollback del perdedor la sesión sigue
   usable: consulta, commitea y reserva de nuevo (09:30) correctamente.

## 11. Tests

`tests/test_booking.py` — 28 tests, todos contra PostgreSQL real usando los fixtures existentes
(`migrated_engine`, `session`, `clean_tables` autouse). Las sesiones extra de los tests concurrentes salen de un
`sessionmaker` ligado al mismo `migrated_engine`. Cero hooks de test en código de producción.

| Grupo | Tests |
|---|---|
| Reserva válida | `test_valid_booking_persists_one_confirmed_appointment`, `test_booking_persists_lead_service_practitioner_and_location` |
| Duración autoritativa | `test_duration_comes_from_catalog_and_caller_cannot_override` (45 min → 09:00–09:45; `duration_minutes`/`end` → `TypeError`) |
| Existencia | `test_missing_lead_raises_not_found`, `test_missing_referenced_entity_raises_not_found[service_id/location_id/practitioner_id]` |
| Estado activo | `test_inactive_service_...`, `test_inactive_location_...`, `test_inactive_practitioner_raises_entity_inactive` |
| Capacidad | `test_missing_capability_...`, `test_inactive_capability_raises_capability_missing` |
| Disponibilidad | `test_start_outside_recurring_availability_...`, `test_start_off_the_fifteen_minute_grid_...`, `test_interval_extending_past_availability_end_...`, `test_start_intersecting_schedule_block_...`, `test_collision_with_confirmed_appointment_is_rejected_by_preflight`, `test_partial_overlap_with_confirmed_appointment_is_rejected_by_preflight` |
| Semántica half-open | `test_cancelled_appointment_does_not_block_rebooking_same_interval`, `test_back_to_back_booking_on_half_open_boundary_succeeds` (09:00–09:30 + 09:30–10:00) |
| Contrato de tiempo | `test_naive_start_is_rejected_as_invalid_input`, `test_equivalent_instant_in_another_zone_books_the_same_utc_interval` |
| Auditoría | `test_successful_booking_writes_exactly_one_creation_audit_event`, `test_audit_event_records_supplied_actor`, `test_failed_booking_writes_neither_appointment_nor_audit_event` |
| Concurrencia | los 3 descritos en §10 |

**Suite enfocada:** `.venv/bin/python -m pytest tests/test_booking.py -q` → **28 passed**
(8 corridas consecutivas verdes; los 3 tests de concurrencia, 30 corridas verdes tras el rediseño).

**Suite completa:** `.venv/bin/python -m pytest -q` → **89 passed** (61 previos + 28 nuevos), verde en 4 corridas.

**Regresión de contratos previos:** los 61 tests anteriores pasan sin cambios; no se editó ningún archivo existente,
así que las superficies compartidas (migración, modelos, errores, availability, conftest) están diff-vacías.

**MediStock:** intacto, nunca abierto ni referenciado.

## 12. Decisiones

1. **Preflight practitioner-wide, no por sede** (§6): alinea el preflight con el alcance real del GiST.
2. **Capability ausente ≡ inactiva** → un solo `CAPABILITY_MISSING`.
3. **Ventana = intervalo pedido** en `generate_slots`: una sola pregunta ("¿es este intervalo exacto reservable?")
   en vez de reimplementar comparaciones de intervalos.
4. **`record_event` no commitea**: la atomicidad la decide el caso de uso; sirve tal cual para Task 9
   (cancelar/reprogramar) con `before_state`/`after_state`.
5. **Actor por defecto `system`**: las columnas son NOT NULL y el borde HTTP aún no tiene identidad (Task 8).
6. **La sesión debe llegar ociosa**: se prefiere fallo ruidoso a unirse en silencio a una transacción ajena.
7. **`flush()` explícito antes del audit**: fija el id y hace que el GiST se pronuncie dentro del bloque.

## 13. Riesgos y pendientes

1. **BLOQUEANTE PARA TASK 8 — `40P01` no está mapeado.** Bajo carrera real, el perdedor puede recibir
   `deadlock_detected` (`40P01`, `OperationalError`) en vez de `23P01`. `app/errors.py` (Task 3) sólo mapea
   `IntegrityError` con `23P01` → 409; hoy un `40P01` saldría como **500**, no como `APPOINTMENT_CONFLICT`.
   No lo corregí aquí porque `app/errors.py` está fuera de mis paths permitidos. Opciones para el orquestador:
   (a) extender el handler de Task 3 para mapear `40P01` en el path de booking a `APPOINTMENT_CONFLICT` 409;
   (b) un reintento único del caso de uso ante `40P01` (la reserva del rival ya estará committeada, así que el
   reintento terminará en `SLOT_BLOCKED` limpio); (c) serializar por practitioner con
   `pg_advisory_xact_lock(practitioner_id)` al inicio de la transacción, lo que elimina el deadlock y convierte al
   perdedor en `SLOT_BLOCKED`, dejando al GiST como respaldo. **(c) cambia la semántica prescrita** ("GiST es la
   autoridad de concurrencia, el perdedor recibe 23P01"), por eso no la apliqué: es decisión del orquestador.
2. **Duplicados por lead**: nada impide que un mismo lead tenga dos citas confirmadas simultáneas con
   practitioners distintos. La spec no lo prohíbe; el GiST sólo cubre practitioner.
3. **Sin horizonte ni lead time mínimo**: se puede reservar en el pasado si cae en disponibilidad. Es una de las
   *Deferred Questions* de la spec (no inventé política).
4. **Sin `SELECT ... FOR UPDATE`**: no aporta contra filas inexistentes; el GiST es la garantía correcta.
5. **DST**: la evaluación usa `ZoneInfo` de la sede (Task 6); `America/Lima` no tiene DST, así que las zonas con
   DST no están ejercitadas end-to-end en este vertical.

## 14. Sin bloqueos para arrancar Task 8

Nada de lo anterior impide empezar Task 8; el punto 13.1 debe resolverse **antes** de considerar cerrado el
contrato HTTP de conflictos.

## 15. Task 8 recomendada — routers + OpenAPI

- `POST /appointments` → `book_appointment` (body: lead/service/location/practitioner/start; **sin** duración),
  `correlation_id` desde header, `actor_*` desde el contexto de request.
- `GET /slots` sobre `generate_slots` + filtro de practitioners elegibles (Task 4), para que el cliente elija un
  slot ya validado.
- Superficies administrativas para services/locations/practitioners/capabilities/availability/blocks.
- Cerrar 13.1 al escribir el handler del path de booking; documentar `404/409/422` con el envelope de Task 3 en OpenAPI.
- Task 9 después: cancelar/reprogramar reusando `record_event` con `before_state`/`after_state`.
