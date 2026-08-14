# Task 10 — Cierre E2E del Vertical 1: Lead-to-Appointment (handoff)

**Fecha:** 2026-08-13 · **Baseline SHA:** `e1d956c` (172 tests PASS) · **Estado:** completo, sin commitear.
**Naturaleza:** tarea PROOF — sin cambios de código de producción.

---

## 1. Objetivo de negocio

Probar de extremo a extremo, exclusivamente a través de la API pública HTTP, el recorrido comercial
completo del vertical: lead → elegibilidad → disponibilidad → reserva → reprogramación → cancelación,
con verificación final directa en PostgreSQL y rastro de auditoría coherente. La prueba demuestra que el
producto resuelve el problema de negocio (un paciente llega, se le asigna el profesional correcto en el
momento correcto y su cita vive un ciclo de vida íntegro y auditable) sin exigir ningún cambio de código:
las capacidades ya implementadas de Tasks 2-9 se comportan como una sola cadena.

## 2. Baseline SHA

`e1d956c` (`feat: add appointment cancellation and rescheduling`), **172 tests PASS**.

## 3. Commit resultante

**Pendiente (placeholder).** El WRITER no commitea por instrucción. Orquestador: commit único con los
archivos de §18.

## 4. Viaje HTTP exacto ejecutado (todo a través de la API, PostgreSQL real)

Cliente: `TestClient(create_app())` con `get_db` sobre `odontoflow_test` (conftest `migrated_engine` /
`clean_tables`). Fecha determinista: **2026-09-01** (martes, `weekday == 1`), derivada por aritmética
`>= 2026-09-01`, nunca del reloj de pared. Zona horaria `America/Lima` (UTC-5 fijo). Ventana UTC que
cubre 09:00-12:00 local = `2026-09-01T14:00:00Z` … `17:00:00Z`.

| # | Request HTTP | Respuesta esperada |
|---|---|---|
| 1 | `POST /services` × 2 | `201 ServiceRead` (30 / 45 min) |
| 2 | `POST /locations` × 2 · `POST /practitioners` × 2 · `POST /capabilities` × 2 | `201` cada uno |
| 3 | `GET /practitioners/eligible?service_id=&location_id=` × 3 | Dra. Ana · Dr. Luis · `[]` |
| 4 | `POST /leads` → `GET /leads/{id}` | `201` → `LeadRead`, `commercial_status == 'new'` |
| 5 | `POST /availability-rules` (Ana @ Sede A, 09:00-12:00, día 1) | `201` |
| 6 | `POST /slots/query` (Consulta @ Sede A, ventana) | 10 slots, solo Ana, 15-min, 45 min |
| 7 | `POST /appointments` (slot A) → `POST /slots/query` | `201 confirmed` → slot A y sus 2 vecinos fuera |
| 7b | `POST /appointments` con `duration_minutes`/`end`/`state` extra | `422 INVALID_INPUT` |
| 8 | `POST /appointments/{id}/reschedule` (slot B) | `200`, mismo id, slot A libre / B ocupado |
| 8b | `POST /appointments/{id}/reschedule` con `state` extra | `422 INVALID_INPUT` |
| 9 | `POST /appointments/{id}/cancel {}` | `200 cancelled`, ambos intervalos libres |
| 10 | `POST /appointments` (Eval @ Sede A con Ana, sin capacidad) | `409 CAPABILITY_MISSING` |
| 10b | `POST /appointments/999999/{reschedule,cancel}` | `404 NOT_FOUND` |

## 5. Recursos creados

- **Servicios:** `Evaluacion Inicial` (30 min), `Consulta Ortodoncia` (45 min).
- **Sedes:** `Sede A`, `Sede B` — ambas `America/Lima`.
- **Practitioners:** `Dra. Ana`, `Dr. Luis`.
- **Capacidades:** `Ana × Consulta × Sede A`, `Luis × Evaluacion × Sede B`.
- **Lead:** `Juan Perez`, teléfono canónico `+51999000111`, fuente `promotion`, `service_need_id` = Consulta.
- **Regla de disponibilidad:** Ana @ Sede A, `day_of_week=1`, 09:00-12:00 local.
- **Cita:** 1 sola fila (ver §11-12).

## 6. Evidencia de capacidad / elegibilidad

Las capacidades son deliberadamente distintas para probar que la elegibilidad no es genérica:

- `Consulta Ortodoncia @ Sede A` → **exactamente** `[Dra. Ana]`.
- `Evaluacion Inicial @ Sede B` → **exactamente** `[Dr. Luis]`.
- `Evaluacion Inicial @ Sede A` → **`[]`** (combinación cruzada vacía).

La consulta reusa `list_eligible_practitioners` (join activo sobre `practitioner_capabilities`); la cita
no se puede crear si el par servicio×sede no está en la capacidad del practitioner (verificado en §14).

## 7. Evidencia de la query de slots

`POST /slots/query` para Consulta @ Sede A en la ventana calculada devuelve **10 candidatos**, todos con
`practitioner_id == Dra. Ana`, exactamente iguales a los intervalos derivados de la regla de Task 6:

- Inicio alineado a grilla de 15 min sobre el reloj local (`start.minute % 15 == 0`).
- `end - start == 45 min` para todos (duración del servicio, no del cliente).
- Orden estrictamente cronológico.
- Los intervalos se seleccionan **de la respuesta de la API** (nunca se fabrican): slot A = primer slot
  ofertado (14:00Z), slot B = primer slot distinto tras la reserva (14:45Z).

## 8. Evidencia de la reserva (booking)

`POST /appointments` con el slot A devuelve `201` con `state == 'confirmed'`, `start_utc == slotA.start`,
`end_utc == start + 45 min`, `id` numérico, y los 5 campos de referencia del `AppointmentRead` exactos.
Tras reservar, `POST /slots/query` **deja de ofrecer** el intervalo reservado y todos los candidatos que
lo intersectan (un intervalo de 45 min excluye 3 inicios de grilla: 10 → 7). La transacción de Task 7
commitea cita + auditoría juntas (verificado en DB, §13/§15).

## 9. Evidencia de duración autoritativa

- `AppointmentRead` no contiene `duration_minutes`; `end_utc` siempre deriva de `services.duration_minutes`
  (45 min) tanto en reserva como en reprogramación.
- El cliente **no puede sobreescribir** `duration`/`end`/`state`: enviar esos campos extra en booking
  (`AppointmentCreate` con `extra='forbid'`) o `state` en reschedule (`AppointmentReschedule` con
  `extra='forbid'`) produce `422 INVALID_INPUT` con el envelope estable.

## 10. Evidencia de reprogramación sobre la MISMA fila

`POST /appointments/{id}/reschedule {new_start=slotB.start}` → `200`, **mismo `id`**, `state` sigue
`confirmed`, `start_utc == slotB.start`, `end_utc == slotB.start + 45 min`. Tras reprogramar: el
intervalo previo (A) vuelve a ofrecerse y el nuevo (B) ya no; sigue habiendo **una sola fila de cita**
(la consulta de slots pasa de 10 → 5: se excluyen los 5 inicios que intersectan el nuevo intervalo de
45 min y se re-incluye A). No se crea ninguna cita de reemplazo ni se ve una transición
"cancelled + confirmed" temporal.

## 11. Evidencia de cancelación / liberación de capacidad

`POST /appointments/{id}/cancel {}` → `200`, **mismo `id`**, `state == 'cancelled'`, `start/end`
**preservados** (el intervalo reprogramado B). El GiST parcial (`state='confirmed'`) deja de consumir el
intervalo al commitear: `POST /slots/query` vuelve a ofrecer **los 10 intervalos originales**, incluidos
A y B.

## 12. Estado final de la Appointment

```
id            = <el id de la reserva>            (único, nunca reemplazado)
lead_id       = id del lead Juan Perez
service_id    = Consulta Ortodoncia
practitioner_id = Dra. Ana
location_id   = Sede A
start_utc     = 2026-09-01T14:45:00Z   (intervalo reprogramado)
end_utc       = 2026-09-01T15:30:00Z   (= start + 45 min)
state         = cancelled
```

## 13. Secuencia de auditoría

`audit_events` filtrados por `entity_type='appointment'` y `entity_id=str(id)`, ordenados por `id`:

| Orden | action | before_state | after_state |
|---|---|---|---|
| 1 | `appointment.created` | `None` | `{start: A, end: A+45, state: confirmed}` |
| 2 | `appointment.rescheduled` | `{start: A, ...}` | `{start: B, end: B+45, state: confirmed}` |
| 3 | `appointment.cancelled` | `{start: B, state: confirmed}` | `{start: B, state: cancelled}` |

- Exactamente **una** de cada acción (conteo total de eventos para el id == 3).
- El evento `rescheduled` documenta el intervalo ORIGINAL en `before_state` y el NUEVO en `after_state`.
- Orden `created → rescheduled → cancelled` garantizado por el monotónico `id` de la secuencia.

## 14. Test negativo de dominio

- `POST /appointments` para `Evaluacion Inicial @ Sede A` con **Dra. Ana** (carece de esa capacidad) →
  `409`, envelope **exacto** `{"error": {"code": "CAPABILITY_MISSING", "message": <str>, "details": {}}}`
  (solo esas claves, sin fuga de internals). La DB queda sin cita y sin audit events.
- `POST /appointments/999999/reschedule` y `/cancel` → `404 NOT_FOUND` con el mismo envelope estable.

## 15. Verificación directa en PostgreSQL (solo lecturas)

Tras el viaje completo: **1 lead**, **2 services**, **2 locations**, **2 practitioners**,
**2 capabilities**, **exactamente 1 fila de appointment** con `state='cancelled'` y el intervalo
reprogramado; **0 filas `confirmed`** (nunca hubo reserva duplicada ni cita de reemplazo). El cliente E2E
no escribe en DB: todas las acciones de negocio son HTTP; SQLAlchemy se usa únicamente para las lecturas
de verificación y la evidencia de auditoría.

## 16. Resultado de la suite enfocada

`.venv/bin/python -m pytest tests/test_lead_to_appointment_e2e.py -q` → **2 passed**
(`test_lead_to_appointment_e2e_full_journey`, `test_negative_capability_missing_and_missing_resource_envelope`).
3 corridas consecutivas verdes.

## 17. Resultado de la suite completa

`.venv/bin/python -m pytest -q` → **174 passed** (172 previos + 2 nuevos), sin fallos ni regresiones.

## 18. Archivos de producción modificados

**Ninguno.** Solo se escribió el archivo de prueba y este documento:

| Archivo | Estado |
|---|---|
| `tests/test_lead_to_appointment_e2e.py` | **nuevo** — E2E del vertical (2 tests) |
| `docs/superpowers/handoffs/2026-08-13-task-10-lead-to-appointment-e2e-handoff.md` | **nuevo** — este informe |

No se detectó ningún defecto de integración real: la única discrepancia durante el desarrollo del test fue
una expectativa incorrecta del WRITER (conteo de slots tras reservar), no un bug del producto.

## 19. Migraciones sin cambios

`alembic/versions/0001_lead_to_appointment.py` intacta (`git diff --stat` vacío); el esquema es idéntico
al baseline y la suite completa es verde sobre la misma migración.

## 20. MediStock intacto

No se abrió, no se referenció, no se modificó.

## 21. Limitaciones conocidas del vertical

1. **Un lead, muchas citas:** el GiST solo cubre practitioner; un mismo lead puede tener citas con
   practitioners distintos (riesgo heredado de Task 7, no prohibido por la spec).
2. **Sin horizonte / lead time:** se puede reservar en el pasado si cae dentro de disponibilidad
   (Deferred Question heredada).
3. **Actor y correlation id** no se cablean aún desde headers HTTP (default `system`); la próxima tarea
   debería exponerlos.
4. **Cancelación de cita cancelada** → `409 ENTITY_INACTIVE` (conflicto estable, no idempotente) — decisión
   documentada en Task 9.
5. **Un solo practitioner por slot y sin multi-location simultánea** fuera del scope del vertical 1.
6. **TestClient/httpx deprecation**: aviso de Starlette; no afecta el resultado.

## 22. Declaración

**Vertical 1 — Lead-to-Appointment: CLOSED.**

## 23. Siguiente actividad

**Platform Readiness Gate — Multi-Tenant & Agent-Native Foundation.** (No se recomienda Clinical Bridge
directamente: el vertical 1 está cerrado y el próximo umbral de valor es aplanar el camino hacia un
producto multi-tenant y apto para agentes — aislar organización/sede por tenant, cablear actor y
correlation id, y madurar la superficie para que agentes operen el ciclo de vida.)
