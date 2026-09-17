# odontoflow-backend — DEVELOPMENT

## Qué es este repo

**El núcleo determinista de OdontoFlow.** FastAPI + PostgreSQL. Es el único
lugar donde vive la verdad de negocio: agenda, clínica, dinero e inventario.
Ningún LLM decide nada acá — cada regla que importa (no doble reserva,
integridad de tenant, no sobrepago) está forzada por PostgreSQL o por código
determinista, nunca por un modelo.

**Estado verificado (2026-09-17):** migración HEAD `0019_sandbox_provider`
contra PostgreSQL real. El conteo de tests actual **no** se fija aquí a
propósito — hay tres cifras históricas que no coinciden (384/403/492) y esta
pasada de documentación no volvió a correr el suite; el número verificado
más reciente vive en `../odontoflow-planning/STATUS.md`. Detalle de arquitectura
en `../odontoflow-planning/docs/ARCHITECTURE.md`.

## Función de desarrollo

Este repo modela el dominio de la clínica con capas verticales
(`app/scheduling`, `app/clinical`, `app/economics`, `app/inventory`,
`app/commercial`, `app/iam`, `app/audit`, `app/idempotency`). Cada vertical
sigue el mismo patrón en su `service.py`: **permiso → idempotencia → regla de
dominio → evento de auditoría, una sola transacción.**

Desarrollar acá significa extender ese patrón, nunca saltárselo.

## Cómo arrancar

```bash
docker start odontoflow-db-1        # PostgreSQL 15 en :5434
uv sync --locked                    # entorno reproducible desde uv.lock
uv run alembic upgrade head         # migración HEAD: 0019_sandbox_provider
uv run python -m pytest -q          # ver ../odontoflow-planning/STATUS.md para el conteo verificado actual
```

Nunca `docker compose up` desde aquí — el nombre del proyecto compose se
deriva del directorio y crea un volumen vacío distinto al de siempre.

## Contrato de trabajo — lee `AGENTS.md` primero

TDD real: tests que fallan primero, contra PostgreSQL real, nunca SQLite.
Un commit por tarea. `main` se queda verde siempre. El archivo `AGENTS.md`
en la raíz tiene el contrato completo, incluida la superficie que **no se
toca sin que la tarea lo pida explícitamente**: `app/errors.py`,
`app/db.py`, `app/scheduling/availability.py`, migraciones existentes.

## Lo que falta construir — priorizado por valor real, no por lo técnicamente interesante

Verificado y confirmado ausente (no es que esté oculto, no existe):

1. **Un campo de precio en `Service`** — hoy no hay tarifario, solo un
   snapshot por ejecución. Es el cambio de esquema más pequeño posible y el
   que más desbloquea (ver la actividad #1 de la discovery).
2. **Ningún dato real de clínica cargado** — el bloqueador más grande del
   proyecto hoy no es código, es conseguir el catálogo y una semana real de
   citas de una clínica de verdad (`M5_REVENUE_LEAKAGE_BASELINE.md §6`).
3. **El agente de ventas (AIRY, `sales_agent/`) ya existe y nunca tiene
   autoridad de negocio propia** — llama a esta API solo a través de un
   gateway de 7 herramientas tipadas (`POST /agent-tools/call`), nunca
   escribe directo a la base. Verificado hasta ahora solo contra un canal
   sandbox de desarrollo (`provider=sandbox`); no hay canal WhatsApp real, no
   hay presupuesto de modelo de producción aprobado, y la confirmación de
   reserva iniciada por el agente queda fail-closed a propósito. Ver
   `../odontoflow-planning/docs/ARCHITECTURE.md` y
   `docs/handoffs/plans/2026-09-17-reception-core-closeout-01.md` (en
   `../odontoflow-planning`) para el estado completo.

No construyas Promotion/Discount/Campaign/Referral/Tariff completos todavía
— no hay evidencia de que la clínica los necesite antes de tener datos
reales.

## Provenance

Escrito por Miguel Arias. Sin contribuciones externas de código en este
repo — las contribuciones de Alejandro y Leonardo viven en
`odontoflow-voice` y `odontoflow-sim` respectivamente (créditos completos
en `../odontoflow-planning/CONTRIBUTIONS.md`).
