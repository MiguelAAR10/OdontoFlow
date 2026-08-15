# BRIEFING DEL ORQUESTADOR — OdontoFlow Lead-to-Appointment (estado actual)

> Para quien dirige y redacta los prompts de los builders. Basado en evidencia verificada el 2026-08-13 (suite completa 61 PASS).

---

## 1. ESTADO DEL REPOSITORIO

**Repo:** `/home/miguel/projects/portfolio/AI-EdgeRunners/OdontoFlow` — branch `main`, tree limpio.

| Commit | Contenido |
|---|---|
| `3504b66` | Task 1: semilla (FastAPI `/health`, pytest, compose Postgres en puerto **5434**) |
| `58c3655` | Task 2: persistencia (config, db, modelos, Alembic 0001 + exclusión GiST) |
| `ce4757f` | Task 2 handoff |
| `084a8d5` | Task 3: contrato de errores (envelope estable) |
| `6069ab5` | Task 4: catálogo operacional + elegibilidad |
| `efc87a8` | Task 5: Lead comercial |
| `92bceed` | Task 6: motor de disponibilidad puro |

**Tests:** `python -m pytest -q` → **61 PASS** (todas las regresiones verdes). DB de test: `odontoflow_test` en `127.0.0.1:5434` (Postgres 15 vía compose de OdontoFlow). MediStock: **read-only**, limpio en `ef2fffb7`.

---

## 2. ARQUITECTURA Y CONTRATOS CANÓNICOS (no negociables)

**Envelope de error (Task 3, `app/errors.py`)** — el builder SIEMPRE usa `AppError(ErrorCode.X, msg)`:
```json
{"error": {"code": "APPOINTMENT_CONFLICT", "message": "...", "details": {}}}
```
- `INVALID_INPUT`→422 · `NOT_FOUND`→404 · `ENTITY_INACTIVE`/`CAPABILITY_MISSING`/`SLOT_BLOCKED`/`APPOINTMENT_CONFLICT`→409.
- `23P01` → `APPOINTMENT_CONFLICT` automático (handler ya registrado). Nunca filtrar SQL/constraints/stacks.

**Base de datos (Task 2, migración `0001` — NO MODIFICAR):**
- 9 tablas: `leads, services, locations, practitioners, practitioner_capabilities, availability_rules, schedule_blocks, appointments, audit_events`.
- **Invariante crítico:** `EXCLUDE USING gist (practitioner_id WITH =, tstzrange(start_utc,end_utc,'[)') WITH &&) WHERE (state='confirmed')` — dos citas confirmadas del mismo practitioner jamás se solapan; canceladas nunca bloquean. SQLSTATE `23P01`.
- CHECKs: `leads.acquisition_source IN (promotion,referral,direct)`, `leads` requiere teléfono O email, `services.duration_minutes > 0`, `appointments.state IN (confirmed,cancelled)`, `end > start`.
- UNIQUE: `services.name`, `practitioner_capabilities(practitioner,service,location)`.

**Reglas de dominio (spec aprobada, no reabrir):**
- Duración SIEMPRE del catálogo (`services.duration_minutes`); el cliente nunca la provee.
- Disponibilidad: grilla de 15 min en la zona horaria de la sede; ventanas `[start,end)`; bloques y citas confirmadas excluyen; canceladas no bloquean.
- Solo `confirmed` consume disponibilidad. `completed/no_show` reservados para vertical clínico (NO implementar).
- Sin LLM/WhatsApp/Google Calendar/NubeFact/Finance/Inventory en este vertical. MediStock es referencia de comportamiento, no código.

---

## 3. MÓDULOS EXISTENTES (para citar en prompts)

| Módulo | Archivos | Funciones públicas |
|---|---|---|
| `app/catalog` | `models.py` (Service), `schemas.py` (ServiceCreate: name, duration_minutes>0, is_active), `service.py` | `create_service(session, data)`, `list_services(session)` |
| `app/organization` | `models.py` (Location, Practitioner, PractitionerCapability), `schemas.py`, `service.py` | `create_location`, `create_practitioner`, `create_capability`, `list_eligible_practitioners(session, service_id, location_id)` |
| `app/commercial` | `models.py` (Lead), `schemas.py` (LeadCreate), `service.py` | `create_lead(session, data)`, `get_lead(session, lead_id)`; `_normalize_phone` puro |
| `app/scheduling` | `models.py` (AvailabilityRule, ScheduleBlock, Appointment), `availability.py` (**puro**, stdlib) | `generate_slots(rules, blocks, appointments, duration_minutes, window_start, window_end, timezone) -> list[(start,end)]`; dataclasses `AvailabilityRule`, `ScheduleBlock`, `Appointment` |
| `app/audit` | `models.py` (AuditEvent) | tabla lista; servicio de auditoría pendiente |
| `app/errors.py` | — | `ErrorCode`, `AppError`, `register_error_handlers(app)` |
| `app/db.py` | — | `engine`, `SessionLocal`, `Base`, `get_db` |

**Fixtures de test (`tests/conftest.py`):** `migrated_engine` (crea/resetea `odontoflow_test`, corre Alembic head), `clean_tables` (autouse, trunca tras cada test), `session`. NOTA: tras un `IntegrityError` hay que `session.rollback()` antes de seguir usando la sesión (los tests previos son ejemplo).

---

## 4. LECCIONES DE EJECUCIÓN (para no repetir errores)

1. **El TUI de opencode crashea (Bun 1.3.14)** en paneles de Herdr → **usar modo headless**: `opencode run --model opencode-go/deepseek-v4-flash "<prompt>"` vía `herdr pane run`. El prompt va en un archivo (`$(cat /tmp/opencode/taskN-prompt.md)`).
2. **Headless auto-rechaza directorios externos** (ej. `../../AI-EdgeRunners/medistock`) → copiar primero los archivos exactos a `/tmp/opencode/medistock-ref/` y apuntar ahí al builder (MediStock queda intacto).
3. Los builders **nunca commitean**; el orquestador hace el fan-in: verificar paths permitidos, suite completa, diff de superficies compartidas vacío, MediStock limpio; luego UN commit por tarea.
4. Un solo builder por tarea, paths permitidos explícitos; nunca dos writers en la misma migración/contrato compartido/`scheduling/service.py`.

---

## 5. PRÓXIMA TAREA — TASK 7: BOOKING TRANSACTION (scope para el prompt)

**Propósito:** caso de uso `book_appointment(session, ...)` que confirma una cita en UNA transacción, apoyándose en el GiST (23P01→409 automático vía Task 3).

**Paths permitidos para el builder (sugeridos):**
- `app/scheduling/service.py` (BookAppointment)
- `tests/test_booking.py`
- handoff `docs/superpowers/handoffs/2026-08-13-task-7-booking-handoff.md`
- NO tocar: migraciones, `availability.py`, `errors.py`, `db.py`, conftest, catálogo/organización/comercial.

**Comportamiento requerido (según spec):**
1. Revalidar dentro de la transacción: servicio activo, sede activa, practitioner activo, capability activa (usar `app/organization/service.py` o queries directas; errores `ENTITY_INACTIVE`/`CAPABILITY_MISSING`).
2. Duración autoritativa desde `services.duration_minutes` (nunca del cliente).
3. Verificar que el slot está en disponibilidad (reusar `generate_slots` de Task 6) y que no cruza bloques.
4. Insertar `Appointment` estado `confirmed`; la exclusión GiST es la garantía final de concurrencia → `23P01` → `409 APPOINTMENT_CONFLICT` (handler Task 3, sin mapeo manual).
5. Escribir `AuditEvent` de creación (actor, acción `appointment.created`, entity, UTC, before/after, correlation_id opcional) en la misma transacción.
6. No implementar cancelación/reprogramación (Task 9).

**TDD cases mínimos (pedir):**
1. reserva válida persiste con start/end calculados;
2. duración de catálogo usada (no input de cliente);
3. servicio inactivo → `ENTITY_INACTIVE`;
4. capability ausente → `CAPABILITY_MISSING`;
5. slot fuera de disponibilidad → `SLOT_BLOCKED`;
6. carrera de dos reservas concurrentes (dos sesiones/threads) → exactamente una persiste, la otra 409;
7. se escribe audit event en la misma transacción;
8. la reserva ocupa el slot (generate_slots ya no lo ofrece).

**Después de Task 7:** Task 8 (routers/OpenAPI) y Task 9 (cancelar/reprogramar + auditoría).
