# Task 9 — Cancelación + Reprogramación (handoff)

**Fecha:** 2026-08-13 · **Baseline SHA:** `f812c04` (122 tests PASS) · **Estado:** completo, sin commitear.

---

## 1. Objetivo

Implementar los dos casos de uso que faltaban del ciclo de vida de la cita: `cancel_appointment` y
`reschedule_appointment`. Ambos en UNA transacción, con la fila bloqueada `FOR UPDATE` como primera lectura,
revalidación autoritativa in-transaction, exactamente UN `AuditEvent` por operación con `before_state`/`after_state`,
y el GiST parcial como autoridad final de concurrencia. Reprogramar mueve la MISMA fila: nunca existe un estado
visible "cancelada vieja + confirmada nueva".

## 2. Baseline y archivos escritos

Baseline: `f812c04` — `.venv/bin/python -m pytest -q` → **122 passed** (verificado antes de escribir nada).

| Archivo | Estado |
|---|---|
| `app/scheduling/service.py` | **modificado** — `cancel_appointment`, `reschedule_appointment`, `_lock_appointment`, `_require_confirmed`, `_appointment_state`, self-exclusión en `_availability_inputs` |
| `app/scheduling/schemas.py` | **modificado** — `AppointmentCancel`, `AppointmentReschedule` |
| `app/scheduling/router.py` | **modificado** — `POST /appointments/{id}/cancel`, `POST /appointments/{id}/reschedule` |
| `tests/test_cancellation.py` | **nuevo** — 13 tests |
| `tests/test_rescheduling.py` | **nuevo** — 37 tests |
| `docs/superpowers/handoffs/2026-08-13-task-9-cancel-reschedule-handoff.md` | **nuevo** — este documento |

`app/audit/service.py` **no** se tocó: `record_event` ya soportaba `before_state`/`after_state` y no commitea, que es
exactamente lo que Task 9 necesita. `git status` sólo muestra los tres archivos modificados arriba + los untracked.
Sin commits.

**Intactos (verificado por `git status`):** migración `0001`, todos los `models.py`, `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py`, contrato de booking de Task 7, contratos de commercial/catalog/organization,
`tests/conftest.py` y todos los tests existentes.

## 3. Contrato de cancelación

```python
cancel_appointment(
    session: Session,
    appointment_id: int,
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> Appointment
```

1. `Appointment` inexistente → `AppError(NOT_FOUND, "Appointment not found.")` (404).
2. Fila cargada `SELECT ... FOR UPDATE` como **primera** sentencia de la transacción.
3. Estado actual debe ser `confirmed`. Si ya está `cancelled` → `AppError(ENTITY_INACTIVE, "The appointment is not
   confirmed and cannot be modified.")` (409). Conflicto **determinista y estable**: repetir la llamada devuelve
   siempre el mismo código (probado). No se inventó un `ErrorCode` nuevo.
4. `state = 'cancelled'`; `start_utc`/`end_utc` **se preservan** (probado en servicio y en API).
5. Exactamente UN `AuditEvent` (§7) en la misma transacción.
6. Commit atómico. Como la exclusión GiST es parcial (`WHERE state = 'confirmed'`), el intervalo queda libre
   inmediatamente y vuelve a ser reservable (probado con un `book_appointment` posterior sobre el mismo intervalo).

## 4. Contrato de reprogramación

```python
reschedule_appointment(
    session: Session,
    appointment_id: int,
    new_start: datetime,          # timezone-aware, obligatorio
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
    correlation_id: str | None = None,
) -> Appointment
```

Orden exacto dentro de la transacción:

1. `FOR UPDATE` de la cita; inexistente → `NOT_FOUND`.
2. Debe estar `confirmed`; si no → `ENTITY_INACTIVE` (mismo conflicto estable que el doble cancel).
3. `new_start` debe ser timezone-aware; naive o no-`datetime` → `INVALID_INPUT` (422).
4. Recarga autoritativa de `Service`/`Location`/`Practitioner` desde la propia cita: ausente → `NOT_FOUND`,
   inactivo → `ENTITY_INACTIVE`. `PractitionerCapability` para *exactamente* practitioner × service × location,
   ausente **o** inactiva → `CAPABILITY_MISSING`. Idénticas reglas y helpers que booking (`_load_active`,
   `_require_capability`): capacidad y estado activo pueden haber cambiado desde que la cita se confirmó.
5. `duration = Service.duration_minutes` (autoritativa); `new_end = new_start + duration`. **No existe** parámetro
   `duration_minutes` ni `end` ni `state`: pasarlos levanta `TypeError` (probado).
6. Reglas, bloqueos y citas confirmadas del practitioner, **excluyendo la propia cita** (§9).
7. `generate_slots(...)` con ventana = intervalo pedido; si `(new_start, new_end)` no está → `SLOT_BLOCKED`.
8. Se actualiza la MISMA fila (`start_utc`, `end_utc`). Sin fila nueva, sin cancelación temporal, sin transición
   visible "cancelada + confirmada" (probado: tras dos reprogramaciones sigue habiendo 1 fila y el historial es
   `created, rescheduled, rescheduled`).
9. `flush()`. El GiST sigue siendo la autoridad final: un `IntegrityError` con SQLSTATE `23P01` **se propaga sin
   traducir** (Task 3 lo mapea a 409 en el borde de transporte).
10. Exactamente UN `AuditEvent` `appointment.rescheduled` con ambos intervalos, en la misma transacción.
11. Commit atómico.

## 5. Estrategia de row locking

`_lock_appointment` es la primera sentencia de ambas transacciones:

```python
session.execute(
    select(Appointment)
    .where(Appointment.id == appointment_id)
    .with_for_update()
    .execution_options(populate_existing=True)
).scalar_one_or_none()
```

- **Sin trampa de autobegin**: se entra en `with session.begin():` *antes* de cualquier lectura (igual que Task 7).
  El caso de uso es dueño de la transacción y debe recibir una `Session` ociosa.
- **`populate_existing=True`** es parte del contrato de bloqueo, no cosmética: con `expire_on_commit=False` el
  identity map puede tener una versión vieja de la fila, y la comprobación de estado debe leer lo que está
  committeado *ahora*, que es justamente lo que el lock garantiza. El perdedor de una carrera decide sobre el estado
  observado **después** de adquirir el lock.
- Consecuencia: dos mutaciones sobre la MISMA cita se serializan en el lock de fila, no compiten por el constraint.
  Por eso Task 9 **no** copia la política de reintento de 40P01 de Task 8: no se observó ningún `40P01` en ninguna de
  las corridas de los tests de concurrencia, y no existe un ciclo de locks documentado que lo justifique. La política
  de Task 8 para booking queda **intacta**; no se añadió manejo global de 40P01.

## 6. Duración autoritativa

`new_end` sale siempre de `services.duration_minutes` leído dentro de la transacción, nunca del llamador. Probado a
nivel de caso de uso (45 min → 10:00–10:45, `TypeError` para `duration_minutes`/`end`/`state`) y a nivel HTTP
(`extra='forbid'` → 422 con envelope `INVALID_INPUT` para `duration_minutes`, `end` y `state`).

## 7. Semántica de auditoría

Un solo `AuditEvent` por operación exitosa, escrito con `record_event` (que sólo hace `session.add`) dentro de la
misma transacción que la mutación.

| | cancelación | reprogramación |
|---|---|---|
| `entity_type` | `appointment` | `appointment` |
| `entity_id` | `str(appointment.id)` | `str(appointment.id)` |
| `action` | `appointment.cancelled` | `appointment.rescheduled` |
| `before_state` | `{id, start_utc, end_utc, state: "confirmed"}` | `{id, start_utc, end_utc, state}` (intervalo viejo) |
| `after_state` | mismos `start/end`, `state: "cancelled"` | `{id, start_utc, end_utc, state}` (intervalo nuevo) |

- Los instantes se serializan con `astimezone(UTC).isoformat()`, así que el payload es canónico en UTC sin depender
  del `TimeZone` de la conexión.
- `actor_id`/`actor_type`: los provistos, o `system`/`system` por defecto (columnas NOT NULL).
- `correlation_id`: sólo si el borde de la petición lo entrega.
- El historial de una cita es encadenable: `after_state` de un evento == `before_state` del siguiente (asertado en el
  test de carrera de dos reprogramaciones).

## 8. Atomicidad transaccional

Mutación + auditoría commitean juntas o no ocurre nada. Probado en ambas direcciones y por dos vías:

1. Fallo de dominio (`SLOT_BLOCKED`, `ENTITY_INACTIVE`, `CAPABILITY_MISSING`, `NOT_FOUND`): la cita queda idéntica y
   **cero** filas de auditoría de la operación.
2. Fallo *después* de la mutación: se sustituye `record_event` por una función que explota (monkeypatch en el test,
   sin hooks en producción); la cita vuelve a su estado anterior y no queda audit huérfano. Esto prueba que la
   mutación no está committeada antes del audit.

## 9. Self-exclusión durante la revalidación de slots

`availability.py` **no se tocó** (sigue puro). La exclusión se aplica en el borde de la consulta/adaptación:

```python
if exclude_appointment_id is not None:
    conflicting = conflicting.where(Appointment.id != exclude_appointment_id)
```

`_availability_inputs` recibe `exclude_appointment_id` como keyword-only con default `None`, así que
`book_appointment` conserva exactamente su semántica de Task 7 (llamada sin ese argumento). El motor de
disponibilidad nunca se entera de qué operación lo invoca.

Probado con un solapamiento consigo misma: una cita 09:00–09:30 se reprograma a 09:15–09:45. Sin self-exclusión el
preflight daría `SLOT_BLOCKED`. Además el `UPDATE` es legal para el GiST: PostgreSQL no considera conflictiva la
versión anterior de la propia fila. También se probó reprogramar al MISMO intervalo (no-op válido).

## 10. Tests de concurrencia (PostgreSQL real, 2+ sesiones independientes, hilos, sincronización determinista, sin `sleep`)

Poller común (nunca duerme, acotado por `monotonic()`):
`SELECT count(*) FROM pg_locks WHERE NOT granted AND pid <> pg_backend_pid()`.
Se cuenta *backends bloqueados*, no locks de relación: un waiter de fila espera en `transactionid`/`tuple`, no en el
lock de tabla, así que el poller de Task 7 (`relname='appointments'`) no serviría aquí.

1. `test_reschedule_defeated_by_a_committed_row_propagates_sqlstate_23P01` — **determinista**. El gate toma
   `LOCK TABLE appointments IN SHARE MODE`: `SHARE` es compatible con `ROW SHARE` (el `SELECT ... FOR UPDATE`) y con
   `ACCESS SHARE` (el preflight), pero conflictúa con `ROW EXCLUSIVE` (el `UPDATE`). Es decir: el worker toma su lock
   de fila, completa **todo** el preflight y ve el slot libre, y queda detenido justo en el `UPDATE`. Recién entonces
   el gate inserta y commitea la fila confirmada rival en el intervalo destino. El `UPDATE` se libera contra una
   entrada ya committeada → `IntegrityError` `23P01` inmediato, propagado sin traducir. La cita original queda
   intacta y sin audit huérfano.
   (La `EXCLUSIVE MODE` de Task 7 no sirve para Task 9: bloquearía el propio `SELECT ... FOR UPDATE` y el hilo ni
   siquiera llegaría al preflight.)
2. `test_two_concurrent_reschedules_of_the_same_appointment_are_serialized` — 2 sesiones independientes, 2 hilos,
   `threading.Barrier(2)`, gate que retiene la fila con `FOR UPDATE`. El principal sondea hasta ver 2 backends
   bloqueados y recién ahí libera. Ambas reprogramaciones commitean, serializadas: queda **1 fila**, estado
   `confirmed`, `start` ∈ {10:00, 11:00} con `end = start + 30 min`, y el historial es
   `created, rescheduled, rescheduled` con encadenamiento coherente (`after` de la primera == `before` de la segunda,
   y el `after` de la última == la fila que sobrevivió). Sin estado intermedio ni fila rota.
3. `test_cancel_and_reschedule_racing_the_same_appointment_settle_coherently` — mismo gate, un hilo cancela y otro
   reprograma. El lock de fila decide, y el test acepta **exactamente** los dos desenlaces posibles:
   - reprogramación primero → ambas commitean; historial `created, rescheduled, cancelled`; fila final `cancelled`
     en 10:00;
   - cancelación primero → la reprogramación observa `cancelled` **después** de adquirir el lock y falla de forma
     determinista con `ENTITY_INACTIVE`; historial `created, cancelled`; fila final `cancelled` en 09:00.

   En ambos casos: 1 sola fila, exactamente un `appointment.cancelled`, y el `after_state` del último evento
   coincide con la fila final. Instrumentando 10 corridas se observaron ambas ramas (7 / 3), así que las dos están
   realmente ejercitadas.

Los 3 tests de concurrencia: **10 corridas consecutivas verdes**, cero flakes.

## 11. Rutas API (estilo Task 8)

| Ruta | Método | Body | Respuesta |
|---|---|---|---|
| `/appointments/{appointment_id}/cancel` | POST | `AppointmentCancel` (vacío, `extra='forbid'`, opcional) | `200 AppointmentRead` |
| `/appointments/{appointment_id}/reschedule` | POST | `AppointmentReschedule` (`new_start` y nada más, `extra='forbid'`) | `200 AppointmentRead` |

- Routers finos: HTTP → schema Pydantic → caso de uso → respuesta tipada. Ninguna consulta previa en el router: los
  casos de uso son dueños de su transacción y reciben la sesión ociosa de `get_db`.
- Se reutiliza el contrato existente `AppointmentRead` (sin cambios).
- Errores con el envelope de Task 3 sin tocar `app/errors.py`: 404 `NOT_FOUND`, 409 `ENTITY_INACTIVE` /
  `SLOT_BLOCKED`, 422 `INVALID_INPUT`. Nada de internals de DB en el cuerpo.
- El body de cancelación es opcional (`AppointmentCancel | None = None`): `POST` sin cuerpo funciona, y un cuerpo con
  campos desconocidos (p. ej. `{"state": "confirmed"}`) da 422 — el cliente no puede colar estado por el body.
- **No se modificó** la política 40P01 específica de booking de Task 8 ni se añadió manejo global de 40P01.

## 12. Tests enfocados

`tests/test_cancellation.py` (13):
`test_cancel_moves_a_confirmed_appointment_to_cancelled`, `test_cancellation_preserves_the_original_interval`,
`test_cancellation_writes_exactly_one_cancelled_audit_event`, `test_cancellation_audit_defaults_to_the_system_actor`,
`test_failed_cancellation_writes_no_audit_and_leaves_the_state_unchanged`,
`test_rejected_cancellation_leaves_neither_mutation_nor_audit`,
`test_cancelled_appointment_releases_the_interval_for_a_new_booking`,
`test_cancel_missing_appointment_raises_not_found`, `test_double_cancellation_raises_a_stable_conflict`,
`test_cancel_endpoint_returns_200_with_the_typed_appointment`,
`test_cancel_endpoint_missing_appointment_returns_404_envelope`,
`test_cancel_endpoint_double_cancel_returns_409_envelope`, `test_cancel_endpoint_forbids_unknown_body_fields`.

`tests/test_rescheduling.py` (37, contando las 3 parametrizaciones de entidad inactiva y las 3 de campos prohibidos):
`test_reschedule_updates_the_same_appointment_row`, `test_new_end_uses_the_canonical_service_duration`,
`test_caller_cannot_supply_duration_or_end_to_the_use_case`, `test_naive_new_start_is_rejected_as_invalid_input`,
`test_appointment_does_not_block_itself_during_revalidation`, `test_rescheduling_to_the_same_interval_is_accepted`,
`test_reschedule_outside_recurring_availability_raises_slot_blocked`,
`test_reschedule_off_the_fifteen_minute_grid_raises_slot_blocked`,
`test_reschedule_into_a_schedule_block_raises_slot_blocked`,
`test_another_confirmed_appointment_blocks_the_new_interval`,
`test_partial_overlap_with_another_confirmed_appointment_is_blocked`,
`test_cancelled_appointment_does_not_block_the_new_interval`, `test_missing_appointment_raises_not_found`,
`test_rescheduling_a_cancelled_appointment_raises_a_stable_conflict`,
`test_inactive_entity_raises_entity_inactive[Service|Location|Practitioner]`,
`test_missing_capability_raises_capability_missing`, `test_inactive_capability_raises_capability_missing`,
`test_successful_reschedule_writes_exactly_one_rescheduled_audit_event`,
`test_reschedule_audit_before_state_holds_the_old_interval`,
`test_reschedule_audit_after_state_holds_the_new_interval`,
`test_failed_reschedule_leaves_the_appointment_and_the_audit_untouched`,
`test_reschedule_mutation_and_audit_commit_together_or_not_at_all`,
`test_reschedule_never_produces_a_cancelled_twin`,
`test_reschedule_defeated_by_a_committed_row_propagates_sqlstate_23P01`,
`test_two_concurrent_reschedules_of_the_same_appointment_are_serialized`,
`test_cancel_and_reschedule_racing_the_same_appointment_settle_coherently`,
`test_reschedule_endpoint_returns_200_with_the_typed_appointment`,
`test_reschedule_schema_forbids_duration_end_and_state[duration_minutes|end|state]`,
`test_reschedule_endpoint_missing_appointment_returns_404_envelope`,
`test_reschedule_endpoint_blocked_slot_returns_409_envelope`,
`test_reschedule_endpoint_cancelled_appointment_returns_409_envelope`,
`test_openapi_exposes_both_task_9_routes`, `test_health_unchanged`.

`.venv/bin/python -m pytest tests/test_cancellation.py tests/test_rescheduling.py -q` → **50 passed**.

## 13. Suite completa y regresión

`.venv/bin/python -m pytest -q` → **172 passed** (122 previos + 50 nuevos). Verde en 9 corridas consecutivas.

**Anomalía transitoria observada una vez (reportada, no reproducida).** En una corrida intermedia la suite completa
terminó `10 failed, 12 errors` con `OperationalError`, afectando también archivos ajenos a Task 9 (p. ej. errores de
fixture en `tests/test_lead.py`). No se reprodujo en 9 corridas completas posteriores ni en 5 corridas enfocadas; el
log de PostgreSQL no registra reinicio, crash ni `FATAL: too many clients` en esa ventana, y tras las corridas no
quedan conexiones filtradas ni backends `idle in transaction` (verificado con `pg_stat_activity`). Todas las sesiones
extra de los tests de concurrencia se cierran en `finally`. Queda anotado como hipótesis de hipo del entorno
(PostgreSQL sobre WSL), no descartado del todo: si el orquestador lo ve repetirse, el primer sospechoso a revisar es
la vida de las conexiones entre procesos de pytest consecutivos.

**Regresión de contratos previos:** los 122 tests anteriores pasan sin modificar ni un solo test existente. Los
cambios en `service.py` son aditivos salvo `_availability_inputs`, que ganó un keyword-only con default `None`, de
modo que la ruta de `book_appointment` es idéntica. `schemas.py` y `router.py` sólo añaden símbolos y rutas;
`AppointmentCreate`/`AppointmentRead` y el retry 40P01 de booking quedaron byte-idénticos.

**Migración:** confirmado sin cambios. `alembic/versions/0001_lead_to_appointment.py` no se abrió para escritura;
Task 9 no requiere DDL: la exclusión GiST parcial `WHERE state = 'confirmed'` y el `CHECK state IN ('confirmed',
'cancelled')` ya existentes soportan cancelar y reprogramar tal cual. `tests/test_migrations.py` y
`tests/test_schema_constraints.py` verdes sin tocar.

**MediStock:** intacto. Nunca se abrió, leyó ni referenció código de MediStock.

## 14. Decisiones

1. **Doble cancelación = conflicto estable, no idempotencia.** Cancelar una cita ya cancelada devuelve
   `ENTITY_INACTIVE` (409) de forma determinista y repetible. Razón: la cancelación idempotente escondería una
   carrera real entre dos actores (recepción y paciente, por ejemplo) y falsificaría el rastro de auditoría — un
   segundo "éxito" sin evento. Un 409 estable le dice al llamador exactamente qué pasó. Se reutiliza
   `ENTITY_INACTIVE` en vez de inventar un `ErrorCode` (fuera de mis paths y, sobre todo, innecesario: la cita está
   inactiva). Esta decisión debería revisarse cuando la spec resuelva su *Deferred Question* sobre política de
   cancelación.
2. **Misma regla para reprogramar una cita cancelada**: `ENTITY_INACTIVE`. Resucitar una cita cancelada moviéndola
   sería una transición no especificada; si el negocio la quiere, es un caso de uso nuevo y explícito.
3. **Self-exclusión en la consulta, no en el motor**: `availability.py` sigue puro e inalterado.
4. **`FOR UPDATE` + `populate_existing`** como base de la serialización de mutaciones sobre la misma cita, en vez de
   copiar el reintento de 40P01 de Task 8 (§5).
5. **La cancelación preserva el intervalo**: no se anula `start_utc`/`end_utc`. El intervalo es el hecho histórico de
   qué se canceló; el GiST parcial ya libera el horario sin necesidad de borrar datos.
6. **Un solo `AuditEvent` por reprogramación** (no `cancelled` + `created`): la spec pide una sola operación atómica
   con ambos intervalos, y dos eventos implicarían que hubo dos estados.
7. **Payload de auditoría normalizado a UTC** con `astimezone(UTC).isoformat()`, independiente del `TimeZone` de la
   conexión.
8. **Body de cancelación vacío pero tipado** (`extra='forbid'`), para que el contrato HTTP sea explícito sobre que
   nada de la cancelación lo decide el cliente.

## 15. Riesgos

1. **Sin política de autorización**: cualquiera que alcance el endpoint puede cancelar o reprogramar cualquier cita.
   La spec lo tiene como *Deferred Question* ("quién puede cancelar o reprogramar"); no inventé roles. `actor_id`/
   `actor_type` existen en el servicio pero el borde HTTP aún no tiene identidad, así que hoy toda operación por API
   queda auditada como `system`.
2. **Sin reglas de negocio de cancelación**: no hay ventana mínima, ni penalidad, ni motivo. `cancel_appointment` no
   acepta `reason` porque el modelo no tiene dónde guardarlo (no toqué migración ni modelos).
3. **Reprogramación al pasado**: igual que booking, si el instante cae dentro de la disponibilidad se acepta. Es la
   *Deferred Question* de lead time / horizonte.
4. **Reprogramación limitada al mismo practitioner/servicio/sede**: mover una cita a otro profesional o sede es un
   caso de uso distinto (no está en el alcance de Task 9). Hoy `new_start` es lo único que cambia.
5. **`NOT_FOUND` de service/location/practitioner en reprogramación no es alcanzable por test**: las FKs son
   `ondelete="RESTRICT"`, así que esas filas no pueden desaparecer bajo una cita existente. El camino está
   implementado (vía `_load_active`) pero sólo se ejerce el sub-caso "inactivo" (`ENTITY_INACTIVE`).
6. **DST**: la evaluación usa `ZoneInfo` de la sede; `America/Lima` no tiene DST, así que una reprogramación que
   cruce un cambio de hora no está ejercitada end-to-end.
7. **Los tests deben capturar `appointment.id` en un `int` antes de compartir la cita entre hilos** (o después de un
   `rollback` que expire el objeto): tocar un atributo expirado dispara un refresh en la sesión de origen, lo que
   reabre una transacción y rompe el contrato de "sesión ociosa" (o genera uso concurrente de la sesión). Es una
   propiedad del contrato de Task 7, no un bug; queda anotado porque costó dos fallos durante el desarrollo.

## 16. Bloqueos

**Ninguno.** El riesgo `40P01` que Task 7 dejó abierto ya lo cerró Task 8 para el path de booking, y Task 9 no lo
reintroduce: las mutaciones sobre la misma cita se serializan por lock de fila (cero `40P01` observados en 10
corridas de la batería de concurrencia).

## 17. Task 10 recomendada

1. **Autenticación + modelo de roles en el borde HTTP** y propagación de `actor_id`/`actor_type` (y `correlation_id`
   desde header) a los tres casos de uso. Es el mayor hueco: la auditoría ya está lista y hoy escribe `system`.
   Cierra además la *Deferred Question* de quién puede cancelar o reprogramar.
2. **Lecturas de cita y de auditoría**: `GET /appointments/{id}`, listado por lead/practitioner/rango, y
   `GET /appointments/{id}/audit` para exponer el rastro que ya se está escribiendo.
3. **Política de lead time y horizonte** (`SLOT_BLOCKED` o un código nuevo), aplicable de forma uniforme a booking y
   a reprogramación desde un único predicado.
4. **Escenario end-to-end multi-sede/multi-profesional** exigido por la Definition of Done: reservar, reprogramar y
   cancelar a través de dos sedes con capacidades distintas.
