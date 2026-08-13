# OdontoFlow Lead-to-Appointment Vertical — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first OdontoFlow vertical — a commercial lead becomes a confirmed multi-location appointment through deterministic FastAPI behavior with database-enforced overlap safety.

**Architecture:** Modular monolith, one FastAPI deployment backed by PostgreSQL. Five explicit module boundaries: `commercial` (Lead), `catalog` (Service), `organization` (Location, Practitioner, PractitionerCapability), `scheduling` (AvailabilityRule, ScheduleBlock, Appointment + booking use cases), `audit` (append-only events). Availability, duration, and conflicts are deterministic; no LLM, WhatsApp, Google Calendar, NubeFact, Finance, or Inventory in this vertical.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (sync) + psycopg v3, Alembic, Pydantic v2, pytest, Docker Compose (PostgreSQL 15+ with `btree_gist`), git.

**Approved spec:** `docs/superpowers/specs/2026-08-12-lead-to-appointment-design.md` (approved; do not reopen architecture).

---

## 1. CURRENT REALITY

- **OdontoFlow repository:** inspected 2026-08-13 UTC. **It is NOT a git repository** (`git rev-parse --is-inside-work-tree` fails; no `.git`, no `.gitignore`, no tracked files). `exact_sha_or_null: null`. Tree contains only `docs/` (spec + evidence). There is **zero** application code, no backend, no frontend, no tests, no migrations, no database config, no tooling. Everything required by the spec is net-new; nothing to preserve, migrate, or refactor in the target tree.
- **MediStock reference:** sibling repo, branch `main`, HEAD `ef2fffb7a348aa621f7a5b387e09a1553351000f`, clean. Used **only** as behavioral reference; its application code is never modified.
- **Confirmed reusable (as patterns only):** see section 7. No MediStock code is copied.
- **Missing pieces (all):** git repo, FastAPI scaffold, DB layer, Alembic migration with `btree_gist` partial GiST EXCLUDE, all 8 entities, slot computation, booking transaction, error contract, audit, tests, OpenAPI, CI/demo.

## 2. TARGET VERTICAL

```
Lead → Service → Practitioner capability → Location → Availability → Slot → Appointment → Cancel / Reschedule
```

Concrete flows: (a) register lead (name + phone or email, source `promotion|referral|direct`, service need); (b) admin create active services (authoritative duration), locations (IANA tz), practitioners, capabilities; (c) define recurring availability + exceptional blocks; (d) query slots on a 15-minute grid in location tz (fit wholly in availability, no block intersection, no confirmed-appointment intersection); (e) book in ONE transaction: reload authoritative duration, revalidate capability/availability, rely on partial GiST exclusion; `23P01` → stable `409`; (f) cancel (releases interval) and reschedule (one atomic transition, one audit record with old/new intervals).

## 3. EXACT CHANGE SURFACE

Proposed OdontoFlow layout (created by this plan, under `/home/miguel/projects/portfolio/AI-EdgeRunners/OdontoFlow`):

```
pyproject.toml              # package + dev deps (fastapi, sqlalchemy, psycopg, alembic, pydantic, pytest, httpx)
.env.example                # DATABASE_URL, TEST_DATABASE_URL, APP_ENV
.gitignore                  # .venv, __pycache__, .env, .pytest_cache
docker-compose.yml          # service db: postgres:15-alpine, btree_gist available; port 5432
alembic.ini
alembic/env.py
alembic/versions/0001_lead_to_appointment.py   # ALL tables + extension + partial EXCLUDE (SHARED WRITE SURFACE)
app/__init__.py             # create_app(): FastAPI factory, exception handlers, routers, OpenAPI (SHARED)
app/config.py               # Settings from env (DATABASE_URL, APP_ENV)
app/db.py                   # engine, SessionLocal, Base, get_db dependency (SHARED)
app/errors.py               # AppError, ErrorCode enum, handlers: 422/404/409/500, 23P01→APPOINTMENT_CONFLICT (SHARED)
app/audit/models.py         # AuditEvent
app/audit/service.py        # record_audit_event(...)
app/commercial/models.py    # Lead
app/commercial/schemas.py   # LeadCreate, LeadRead (Pydantic v2)
app/commercial/service.py   # create_lead, get_lead, normalize_lead (two-stage normalize→validate)
app/commercial/router.py    # POST /leads, GET /leads/{id}
app/catalog/models.py       # Service
app/catalog/schemas.py      # ServiceCreate, ServiceRead
app/catalog/service.py      # create_service, list_services
app/catalog/router.py       # POST /services, GET /services
app/organization/models.py  # Location, Practitioner, PractitionerCapability
app/organization/schemas.py # LocationCreate, PractitionerCreate, CapabilityCreate (+ reads)
app/organization/service.py # create_location, create_practitioner, create_capability, list_eligible_practitioners
app/organization/router.py  # POST /locations, /practitioners, /capabilities; GET /practitioners?service_id=&location_id=
app/scheduling/models.py    # AvailabilityRule, ScheduleBlock, Appointment
app/scheduling/schemas.py   # AvailabilityCreate, BlockCreate, SlotQuery, SlotRead, AppointmentCreate/Read
app/scheduling/availability.py  # PURE: generate_slots(rules, blocks, appointments, duration, start, end, tz) — no I/O
app/scheduling/service.py   # BookAppointment, CancelAppointment, RescheduleAppointment (authoritative; SHARED SURFACE)
app/scheduling/router.py    # POST /availability-rules, /schedule-blocks, /slots/query, /appointments; PATCH/DELETE /appointments/{id}
tests/conftest.py           # fixtures: test engine, alembic upgrade head, session factory, httpx TestClient
tests/unit/test_intervals.py
tests/unit/test_availability.py
tests/unit/test_errors.py
tests/integration/test_lead.py
tests/integration/test_catalog_organization.py
tests/integration/test_booking.py
tests/integration/test_concurrency.py
tests/integration/test_cancel_reschedule.py
tests/integration/test_e2e_vertical.py
```

- **Migrations:** one Alembic migration (`0001`) creating all tables; enables `btree_gist`; adds the partial GiST exclusion on `appointments`:
  `EXCLUDE USING gist (practitioner_id WITH =, tstzrange(start_utc, end_utc, '[)') WITH &&) WHERE (state = 'confirmed')`.
- **Contracts affected:** new (no existing): Lead/Service/Location/Practitioner/Capability/AvailabilityRule/ScheduleBlock/Appointment/AuditEvent Pydantic schemas; stable error codes (`INVALID_INPUT`, `NOT_FOUND`, `ENTITY_INACTIVE`, `CAPABILITY_MISSING`, `SLOT_BLOCKED`, `APPOINTMENT_CONFLICT`).
- **Tests affected:** none existing; all net-new.

## 4. IMPLEMENTATION ORDER (business slices, each independently verifiable)

0. **Seed (repo + tooling + smoke test).**
1. **Persistence/migration** (db.py, config, migration 0001, models; no business logic).
2. **Domain/contracts** (errors.py, stable error codes; unit tests).
3. **Catalog + Organization slice** (Service, Location, Practitioner, Capability; test-first).
4. **Commercial slice** (Lead + normalization; test-first).
5. **Availability calculation** (pure `generate_slots`; test-first, no I/O).
6. **Booking transaction** (BookAppointment + 23P01→409; test-first, integration).
7. **API layer** (routers, OpenAPI, handlers).
8. **Cancellation/Rescheduling** (test-first, atomic + audit).
9. **E2E vertical demo** (deterministic scenario test + DoD checklist).

Dependency shape is exactly the one requested; no evidence forces a change.

## 5. CHARACTERIZATION / TDD (test-first, mandatory)

All tests below are written BEFORE their implementation, run to see them fail, then implemented minimally. Required coverage:

- **practitioner capability** — `tests/integration/test_catalog_organization.py`: eligible only when capability active + matches service + location; inactive entities excluded.
- **slot generation** — `tests/unit/test_availability.py`: 15-minute grid; whole interval inside availability; excludes blocks and confirmed appointments; catalog duration; client-supplied duration ignored.
- **service duration** — catalog is the only source; request payloads have no duration field (test asserts override attempt is rejected/ignored).
- **schedule blocks** — slot query excludes any candidate intersecting a block.
- **appointment overlap** — unit interval tests + integration: DB rejection of overlapping confirmed rows.
- **concurrent booking** — `tests/integration/test_concurrency.py`: two sessions race the same slot; exactly one persists; loser observes `23P01`.
- **cancellation releases slot** — after cancel, the same interval is bookable again.
- **atomic rescheduling** — one transition, one audit record, old/new intervals in before/after; no intermediate state visible.
- **23P01 → stable HTTP 409** — `tests/integration/test_booking.py`: mapped to `APPOINTMENT_CONFLICT`, stable code, safe detail.

## 6. PARALLELISM (Miguel, Leo, Developer/Agent 3)

**Sequential gate (no parallel work here):** Slice 0 (seed) and slice 1 (migration + db + models) — SHARED WRITE SURFACE: `pyproject.toml`, `docker-compose.yml`, `alembic/versions/0001`, `app/db.py`, `app/errors.py`. Integration authority: **Miguel** (owns app factory, db, errors, migrations; final merge gate).

**Wave B (parallel, after migration merged):**
- **Leo → commercial slice** (Lead): files `app/commercial/*`, `tests/integration/test_lead.py`. Depends: errors + db + migration.
- **Miguel → scheduling slice** (availability + booking + concurrency): `app/scheduling/availability.py`, `scheduling/service.py`, `tests/unit/test_availability.py`, `test_concurrency.py`, `test_booking.py`. AUTHORITATIVE scheduling service — nobody else edits `scheduling/*`.
- **Dev/Agent 3 → catalog + organization slice**: `app/catalog/*`, `app/organization/*`, `tests/integration/test_catalog_organization.py`. Depends: errors + db + migration.

**Wave C (sequential after B):** API routers (touches shared `app/__init__.py` — one owner, Miguel); then cancellation/rescheduling (Leo or Miguel, after booking merged — depends on scheduling service); then E2E demo (Miguel).

**Rules:** never two owners on the same migration, shared contract, or `scheduling/service.py`. Each slice merges with its own green tests; merge order enforced by the integration authority.

## 7. MEDISTOCK REUSE (evidence: `docs/superpowers/plans/.evidence/lead-appointment-medistock.json`, commit `ef2fffb7`)

| MediStock concept | Disposition | Reason / evidence | Risk |
|---|---|---|---|
| Paciente (patient) | **REFERENCE** | Lead differs (contact channel, source, service need; no DNI). Copy only the `alertas`-style completeness pattern and rule-check-then-persist service shape (`paciente_service.py:29-82`). | DNI assumptions; legacy `query.get` |
| Servicio / ServicioCatalogo | **ADAPT** | Table shape (id + unique name) is a seed for Service; MUST add duration + active, DROP price. Do not copy the duplicate model classes for one table (`servicio.py` + `servicio_catalogo.py`). | latent service-layer bugs (`catalogo_service.py:52,72`) |
| Consulta | **REFERENCE** | Domain vocabulary + ORM relationship style only. No time, no state, no conflict machinery — do not copy shape. | DATE-only granularity misleads |
| BaseModel | **ADAPT** | Keep `update()` hasattr guard + column-inspection `to_dict`; use `session.get()` not `query.get`. Drop camelCase conversion. | camelCase leak |
| Service layer (static-method containers) | **REFERENCE** | dict-in/ORM-out, ValueError-on-rule-breach is a clean boundary; implement as dependency-injected use-case functions/classes. | no DI seam |
| Data curation / normalization | **ADAPT** | Two-stage normalize→validate with `{curated, issues}`; `_normalize_phone` (`re.sub(r'[^\d+]', '', ...)`) transfers directly. Keep pure functions. | `_normalize_dni` is Peruvian-id specific — do not reuse |
| Marshmallow schemas | **REFERENCE** | Create/Update split, `dump_only` system fields, whitelisting → replicate in Pydantic v2. 400/422 distinction differs; align to spec (422). | semantics differ |
| App factory / config / response envelope | **ADAPT** | `create_app` factory + per-env config classes + TIMEZONE-from-env are good precedents; replace Flask/blueprints/envelope with FastAPI patterns and a stable ErrorCode enum (spec contract). | GENERIC_ERROR ad hoc codes |
| SQL migrations / orchestrator | **ADAPT** | Numbered, idempotent, transactional migration style → adopt via Alembic (not the psql script runner). | `src/hidden.py` secrets module — never reuse |
| Tests | **DO NOT USE** | Zero tests exist in MediStock (`no tests collected`; no pytest deps). All characterization tests are written from scratch. | — |

**Never copy from MediStock:** Flask blueprints/`flask_sqlalchemy` session style, duplicate model classes, broken DDL (`01_create_schema.sql:108,118`), `query.get/paginate`, `hidden.py` credentials, hardcoded dev secrets, ad hoc error envelope defaults, camelCase base-model leak, emoji/pedagogical comments.

## 8. DEFINITION OF DONE

The vertical is complete only when a deterministic E2E test (`tests/integration/test_e2e_vertical.py`) demonstrates, against a real PostgreSQL:

Lead → valid active service (catalog duration) → eligible active practitioner (capability for service+location) → valid active location (IANA tz) → available slot (15-min grid, fits availability, no block) → confirmed appointment (transactional).

Plus:
- overlapping concurrent booking is rejected (one persists; loser gets stable 409);
- cancellation releases capacity (interval bookable again);
- reschedule is atomic and audited (one audit record, old/new intervals, same transaction);
- `23P01` maps to stable HTTP 409 with machine-readable code;
- OpenAPI describes public surfaces + error schemas;
- works for ≥ 2 locations and ≥ 2 practitioners with different capabilities;
- no external adapter or LLM on the booking decision path;
- MediStock application code unmodified (`git status` clean there);
- `pytest` green: `python3 -m pytest`.

## 9. EXECUTION TASKS (Notion Kanban)

| # | Task | Outcome | Owner surface | Dependency | Deterministic verification |
|---|------|---------|---------------|------------|---------------------------|
| 1 | Seed repo | `git init`; `pyproject.toml`; `.gitignore`; `.env.example`; FastAPI app factory with `/health`; pytest wired; compose Postgres | Miguel (shared) | — | `docker compose up -d db`; `pytest -q` green; `curl /health` 200; first commit |
| 2 | Config + DB + migration 0001 | `config.py`, `db.py`, Alembic init, migration with `btree_gist` + partial EXCLUDE + all tables | Miguel (shared) | 1 | `alembic upgrade head` against fresh DB; exclusion constraint exists (`\d appointments`) |
| 3 | Error contract | `errors.py`, ErrorCode enum, 422/404/409 handlers | Miguel (shared) | 2 | unit tests on handlers; stable codes |
| 4 | Catalog + Org models/services | Service/Location/Practitioner/Capability CRUD, eligibility query | Dev/Agent 3 | 3 | `test_catalog_organization.py` green (capability, inactive exclusion) |
| 5 | Commercial Lead | Lead model/schema/service/router; normalization pipeline; source validation | Leo | 3 | `test_lead.py` green (valid lead, invalid source 422) |
| 6 | Availability pure logic | `availability.py`: 15-min grid, blocks, confirmed exclusion | Miguel | 4 | `test_availability.py` green (no I/O) |
| 7 | Booking transaction | BookAppointment; DB exclusion enforcement; 23P01→409 | Miguel | 4, 6 | `test_booking.py` + `test_concurrency.py` green |
| 8 | API layer | routers + OpenAPI + error handlers wired | Miguel (shared) | 4, 5, 7 | OpenAPI served; contract tests green |
| 9 | Cancel/Reschedule | atomic transitions + audit records | Leo | 7 | `test_cancel_reschedule.py` green |
| 10 | E2E vertical | full deterministic scenario test | Miguel | 8, 9 | `test_e2e_vertical.py` green; DoD checklist complete |

## 10. NEXT ACTION

**Only:** execute Task 1 — `git init` OdontoFlow, commit the approved spec + evidence, scaffold `pyproject.toml`/`.env.example`/`.gitignore`, FastAPI `/health` + pytest smoke test, `docker-compose.yml` with Postgres, and land the first commit. Do not implement business logic in this task.
