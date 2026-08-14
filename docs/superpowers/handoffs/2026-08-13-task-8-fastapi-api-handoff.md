# Task 8 — FastAPI API Integration (handoff)

**Fecha:** 2026-08-13 · **Baseline SHA:** `952a19b` (89 tests PASS) · **Estado:** completo, sin commitear.

---

## 1. Objetivo

Exponer el vertical lead-to-appointment como API HTTP FastAPI: routers finos sobre los servicios de
aplicación existentes (commercial/catalog/organization), la query de slots de Task 6 reusada, el booking de
Task 7 con su política de reintento ante deadlock (`40P01`) y OpenAPI autogenerado con esquemas tipados. Sin
lógica de negocio en los routers, sin segunda forma de error, sin rediseñar entidades.

## 2. Baseline SHA

`952a19b` (`feat: add transactional appointment booking`), 89 tests PASS.

## 3. Commit resultante

**Pendiente (placeholder).** El WRITER no commitea por instrucción. Orquestador: commit único con los archivos
de §6.

## 4. Rutas añadidas

| Método | Ruta | Handler |
|---|---|---|
| `POST` | `/services` | `catalog.router.create_service_route` |
| `GET` | `/services` | `catalog.router.list_services_route` |
| `POST` | `/leads` | `commercial.router.create_lead_route` |
| `GET` | `/leads/{lead_id}` | `commercial.router.get_lead_route` |
| `POST` | `/locations` | `organization.router.create_location_route` |
| `POST` | `/practitioners` | `organization.router.create_practitioner_route` |
| `POST` | `/capabilities` | `organization.router.create_capability_route` |
| `GET` | `/practitioners/eligible?service_id=&location_id=` | `organization.router.list_eligible_practitioners_route` |
| `POST` | `/availability-rules` | `scheduling.router.create_availability_rule_route` |
| `POST` | `/schedule-blocks` | `scheduling.router.create_schedule_block_route` |
| `POST` | `/slots/query` | `scheduling.router.query_slots_route` |
| `POST` | `/appointments` | `scheduling.router.create_appointment_route` |
| `GET` | `/health` | preexistente (sin cambios) |
| `GET` | `/openapi.json` | autogenerado por FastAPI |

## 5. Contratos request/response

Todas las respuestas son modelos Pydantic (`response_model` explícito; nunca se devuelve ORM sin tipar).
Creaciones → `201`, GETs → `200`. Errores → un solo envelope de `app/errors.py`:
`{"error": {"code", "message", "details"}}` (`422 INVALID_INPUT`, `404 NOT_FOUND`, `409`).

| Ruta | Request | Response |
|---|---|---|
| `POST /services` | `ServiceCreate{name, duration_minutes, is_active}` | `ServiceRead` |
| `GET /services` | — | `list[ServiceRead]` (orden por nombre) |
| `POST /leads` | `LeadCreate{full_name, contact_phone?, contact_email?, acquisition_source, service_need_id?}` | `LeadRead` |
| `GET /leads/{lead_id}` | — | `LeadRead` |
| `POST /locations` | `LocationCreate{name, timezone}` | `LocationRead` |
| `POST /practitioners` | `PractitionerCreate{display_name, is_active}` | `PractitionerRead` |
| `POST /capabilities` | `CapabilityCreate{practitioner_id, service_id, location_id, is_active}` | `CapabilityRead` |
| `GET /practitioners/eligible` | `service_id`, `location_id` (query) | `list[PractitionerRead]` |
| `POST /availability-rules` | `AvailabilityRuleCreate{practitioner_id, location_id, day_of_week 0-6, start_local, end_local}` | `AvailabilityRuleRead` |
| `POST /schedule-blocks` | `ScheduleBlockCreate{practitioner_id, location_id, start_utc, end_utc}` | `ScheduleBlockRead` |
| `POST /slots/query` | `SlotQuery{service_id, location_id, window_start, window_end}` | `list[SlotResult{practitioner_id, start, end}]` |
| `POST /appointments` | `AppointmentCreate{lead_id, service_id, location_id, practitioner_id, start}` (`extra="forbid"`) | `AppointmentRead` |

`AppointmentRead` expone `{id, lead_id, service_id, practitioner_id, location_id, start_utc, end_utc, state}`.

## 6. Archivos escritos

| Archivo | Estado |
|---|---|
| `app/catalog/router.py` | **nuevo** — POST/GET `/services` |
| `app/commercial/router.py` | **nuevo** — POST `/leads`, GET `/leads/{id}` |
| `app/organization/router.py` | **nuevo** — locations/practitioners/capabilities/eligible |
| `app/scheduling/schemas.py` | **nuevo** — esquemas tipados del vertical de scheduling |
| `app/scheduling/query.py` | **nuevo** — `find_available_slots` + helpers de persistencia de rules/blocks |
| `app/scheduling/router.py` | **nuevo** — rules/blocks/slots/booking + política 40P01 |
| `app/__init__.py` | **modificado** — solo registro de los 4 routers en `create_app()` |
| `tests/test_api.py` | **nuevo** — 33 tests de contrato HTTP contra PostgreSQL real |
| `docs/superpowers/handoffs/2026-08-13-task-8-fastapi-api-handoff.md` | **nuevo** — este documento |

Intactos (diff-vacíos): migración `0001`, todos los `models.py`, `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py` (puro), `app/scheduling/service.py` (semántica de booking de Task 7),
`app/commercial/service.py`, `app/catalog/service.py`, `app/organization/service.py`, `tests/conftest.py`
y todos los archivos de test previos.

## 7. Cómo delegan los routers en los servicios de aplicación

Patrón único, en cada router: `Depends(get_db)` → esquema Pydantic de entrada → llamada al servicio de
aplicación existente → retorno tipado con `response_model`. Los routers no contienen reglas de negocio, no
revalidan lo que los servicios ya validan y no abren transacciones propias.

- `POST /services` → `create_service(db, ServiceCreate)`
- `GET /services` → `list_services(db)`
- `POST /leads` → `create_lead(db, LeadCreate)`; `GET /leads/{id}` → `get_lead(db, lead_id)`
- `POST /locations` → `create_location(db, ...)` (valida IANA en el servicio → `422 INVALID_INPUT`)
- `POST /practitioners` → `create_practitioner(db, ...)`
- `POST /capabilities` → `create_capability(db, ...)`
- `GET /practitioners/eligible` → `list_eligible_practitioners(db, service_id, location_id)`
- `POST /availability-rules` → `query.create_availability_rule(db, ...)`
- `POST /schedule-blocks` → `query.create_schedule_block(db, ...)`
- `POST /slots/query` → `query.find_available_slots(db, ...)`
- `POST /appointments` → `scheduling.router.book_appointment_with_retry(db, operation=..., **payload)`

El booking **no hace ninguna consulta preliminar** en el router: `book_appointment` es dueño de su
transacción (`with session.begin()`) y recibe la `Session` ociosa de `get_db`. No se abre una transacción
externa.

## 8. Arquitectura de la query de slots

`find_available_slots(session, service_id, location_id, window_start, window_end)` en `app/scheduling/query.py`:

1. Carga `Service` y `Location` con el mismo contrato de estados que el booking (`NOT_FOUND` si faltan,
   `ENTITY_INACTIVE` si inactivos).
2. Valida ventana: `window_start`/`window_end` deben ser timezone-aware (`422 INVALID_INPUT` si naive) y
   `window_end > window_start`.
3. Reutiliza `list_eligible_practitioners` (organización, contrato intacto) para el set de practitioners.
4. Por practitioner, adapta filas ORM a dataclasses de Task 6 (`_as_rule`/`_as_block`/`_as_appointment`)
   y delega en `generate_slots(rules, blocks, appointments, duration_minutes, window_start, window_end,
   timezone)`. Los `Appointment` se cargan **practitioner-wide** (no por sede), igual que el preflight de
   booking, porque el GiST ignora la sede.
5. Devuelve lista determinista cronológica de `{practitioner_id, start, end}` (datetimes aware, en UTC),
   ordenada por `(start, end, practitioner_id)`.

## 9. El motor de Task 6 permanece puro y reutilizado

`app/scheduling/availability.py` no se tocó. `query.py` y `service.py` (Task 7) lo llaman con adaptadores
ORM→dataclass; ninguna capa duplica el algoritmo de intervalos, la grilla de 15 min ni la semántica half-open.

## 10. Comportamiento del endpoint de booking

`POST /appointments` acepta solo `{lead_id, service_id, location_id, practitioner_id, start}`. Llama a
`book_appointment` existente (sin reproducir validación) y devuelve `201` con `AppointmentRead`. Los errores
de preflight fluyen como `AppError` (`404 NOT_FOUND`, `409 ENTITY_INACTIVE/CAPABILITY_MISSING/SLOT_BLOCKED`,
`422 INVALID_INPUT`) y el `23P01` llega al handler de Task 3 como `409 APPOINTMENT_CONFLICT`. `start` naive o
no-datetime → `INVALID_INPUT` (422).

## 11. `duration`/`end` no pueden ser sobreescritos

- El esquema `AppointmentCreate` usa `model_config = ConfigDict(extra="forbid")`: enviar
  `duration_minutes`, `end` o `state` produce `422 INVALID_INPUT` por `RequestValidationError` (probado en
  `test_client_cannot_override_duration_end_or_state`).
- `book_appointment` no acepta esos parámetros (firma sin `duration_minutes`/`end`), así que tampoco existe
  la vía de override a nivel servicio. La duración canónica sale siempre de `services.duration_minutes`.

## 12. Verificación de `23P01` → `409`

`test_real_23p01_path_returns_409_appointment_conflict`: el request HTTP de booking se **bloquea en su INSERT**
(detrás de `LOCK TABLE appointments IN EXCLUSIVE MODE`, que deja pasar los SELECT del preflight) y recién
entonces se commitea la fila ganadora debajo. El perdedor llega al GiST y recibe `IntegrityError` con SQLSTATE
`23P01`, que el router **no traduce** (solo lo re-evalúa para decidir si reintentar); el handler de
`app/errors.py` lo mapea a `409 APPOINTMENT_CONFLICT`. El test verifica: `status 409`, código
`APPOINTMENT_CONFLICT`, sin fuga de `23P01`/`conflicting key`/`excl_appointments_confirmed_no_overlap`,
exactamente 1 fila de cita y 0 audit events.

## 13. Política exacta de reintento `40P01` (específica del booking)

Helper `book_appointment_with_retry(session, *, operation=None, **kwargs)` en `app/scheduling/router.py`:

1. Llama a `operation(session, **kwargs)` (default `book_appointment`).
2. `IntegrityError`/`OperationalError` con SQLSTATE `23P01` → **re-raise sin tocar** (Task 3 mapea a 409;
   este helper no traduce 23P01).
3. `OperationalError`/`IntegrityError` con SQLSTATE `40P01` (deadlock) en el **primer** intento →
   `session.rollback()` y **un único reintento** de la operación completa (sin sleep, sin librería de loops,
   sin reintento ilimitado).
4. Si el reintento tiene éxito → `201` normal.
5. Si el reintento levanta un conflicto determinista (`23P01`) → fluye (409 vía Task 3).
6. Si un **segundo** `40P01` ocurre → `AppError(ErrorCode.APPOINTMENT_CONFLICT, "The requested appointment
   slot is no longer available.")` → `409` con envelope estable, sin internals de la base.

El helper acepta la operación por inyección (seam testable vía `get_booking_operation` de FastAPI
`dependency_overrides`); `book_appointment` no contiene hooks de test. No se añadió ningún `ErrorCode`, no se
modificó `app/errors.py`, y no se clasifica `40P01` globalmente.

**Verificación:** `test_first_40p01_retries_exactly_once_then_succeeds` (fake que falla 40P01 una vez y
devuelve éxito; `calls == 2`, `201`), `test_40p01_once_then_success_returns_201` (reintento delega en el
`book_appointment` real y persiste la cita), `test_repeated_40p01_returns_409_appointment_conflict_without_db_leaks`
(40P01 dos veces → `409 APPOINTMENT_CONFLICT`, sin `40P01`/`deadlock`/`Traceback` en el cuerpo, `calls == 2`).

## 14. Evidencia de OpenAPI

`GET /openapi.json` (probado en `test_openapi_exposes_all_routes_and_typed_schemas`): expone las 12 rutas de
Task 8 + `/health`, los verbos correctos por ruta, y los 18 esquemas tipados
(`ServiceCreate/Read`, `LeadCreate/Read`, `LocationCreate/Read`, `PractitionerCreate/Read`,
`CapabilityCreate/Read`, `AvailabilityRuleCreate/Read`, `ScheduleBlockCreate/Read`, `SlotQuery`,
`SlotResult`, `AppointmentCreate/Read`).

## 15. Resultado de la suite enfocada

`.venv/bin/python -m pytest tests/test_api.py -q` → **33 passed** (4 corridas consecutivas verdes; el test
concurrente 23P01 y los 3 de 40P01 estables).

## 16. Resultado de la suite completa

`.venv/bin/python -m pytest -q` → **122 passed** (89 previos + 33 nuevos).

## 17. Regresión de tareas previas

Los 89 tests anteriores pasan sin cambios (TDD red→green: `test_api.py` falló en colección con
`ModuleNotFoundError: app.scheduling.router` antes de implementar, y el 100% quedó verde después). Ningún
archivo de test previo fue tocado.

## 18. Migración intacta

`alembic/versions/0001_lead_to_appointment.py` sin cambios; el esquema es idéntico al baseline (verificado
por `git diff --stat` vacío en migración y el hecho de que la suite completa sigue verde sobre el mismo
esquema migrado).

## 19. MediStock intacto

No se abrió, no se referenció, no se modificó.

## 20. Bloqueantes

Ninguno. El bloqueante de Task 7 (§13.1, `40P01` → 500) queda cerrado con la política de reintento de §13 y
su verificación determinista.

## 21. Riesgos

1. **`40P01` con reintento exitoso es raro en carrera real**: bajo la prueba determinista se provoca el
   entrelazado, pero la ocurrencia natural depende de timing de PostgreSQL. El reintento único y el fallback
   a `409 APPOINTMENT_CONFLICT` cubren ambas caras.
2. **`OperationalError` no-40P01** (p. ej. conexión caída) no se reintenta: se propaga como 500. Decisión
   intencional (política sólo de deadlock), no mapeada a 409.
3. **Duplicados por lead** (riesgo heredado de Task 7 §13.2): un mismo lead puede tener citas con
   practitioners distintos; el GiST sólo cubre practitioner. La spec no lo prohíbe.
4. **Sin horizonte/lead time**: se puede reservar en el pasado si cae en disponibilidad (Deferred Question
   heredada).
5. **Correlation id / actor headers** no se exponen aún en HTTP: `book_appointment` recibe
   `actor_id/actor_type/correlation_id` por defecto (`system`). Cablearlos desde headers es el primer paso
   natural de la siguiente tarea.
6. **TestClient deprecation**: Starlette avisa de `httpx` vs `httpx2`; no afecta el resultado ni la suite.

## 22. Task 9 recomendada

Cancelación/reprogramación de citas reusando `record_event` con `before_state`/`after_state`:
`POST /appointments/{id}/cancel` y `POST /appointments/{id}/reschedule` sobre el mismo vertical, con el
mismo envelope, el mismo patrón de router fino y `book_appointment_with_retry` reutilizado para
reprogramar. Cablear `X-Correlation-ID` y `X-Actor-*` desde headers en booking/cancel/reschedule como
plumbing fino de borde.
