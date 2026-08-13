# Task 2 Handoff — Persistence Foundation (Lead-to-Appointment)

**Commit:** `58c3655` (parent: `3504b66` seed)
**Repo:** `/home/miguel/projects/portfolio/AI-EdgeRunners/OdontoFlow`
**Date:** 2026-08-13

## Business objective

Establish the canonical PostgreSQL/SQLAlchemy/Alembic model for the Lead-to-Appointment vertical, including the database-enforced booking invariant: **two confirmed appointments cannot overlap for the same practitioner** (partial GiST exclusion). This is the deterministic foundation Task 3+ build on; no booking services exist yet by design.

## Schema created (migration `0001`, all tables in `public`)

| Table | Purpose | Key constraints |
|---|---|---|
| `services` | Catalog service, authoritative duration | `name` UNIQUE, `duration_minutes > 0` CHECK |
| `locations` | Multi-sede, IANA timezone | `timezone` required |
| `practitioners` | Provider | `is_active` default true |
| `practitioner_capabilities` | practitioner×service×location | UNIQUE(practitioner,service,location); FKs RESTRICT |
| `leads` | Commercial lead | CHECK source IN (promotion,referral,direct); CHECK phone OR email present; optional `service_need_id` FK |
| `availability_rules` | Recurring local-time availability | day_of_week 0-6 CHECK; end>start CHECK |
| `schedule_blocks` | Exceptional closed intervals | end>start CHECK |
| `appointments` | Booking record | state IN (confirmed,cancelled); end>start; **partial GiST EXCLUDE** |
| `audit_events` | Append-only audit | JSONB before/after; index (entity_type,entity_id) |

Extensions enabled: `btree_gist` (required for `=` on `practitioner_id` inside GiST).

## Migration / constraint behavior

- `0001` creates all tables + extension; `downgrade` drops tables (reverse order) and the extension.
- Critical constraint (`appointments`):
  `EXCLUDE USING gist (practitioner_id WITH =, tstzrange(start_utc, end_utc, '[)') WITH &&) WHERE (state = 'confirmed')`
  named `excl_appointments_confirmed_no_overlap`. Cancelled rows never block interval reuse. SQLSTATE on violation: `23P01` (HTTP mapping belongs to Task 3).
- `alembic/env.py` respects an explicitly configured `sqlalchemy.url` (test overrides) and falls back to `DATABASE_URL` from env/settings otherwise.

## Files changed and responsibilities

| File | Responsibility |
|---|---|
| `app/config.py` | Env-driven `Settings` (APP_ENV, DATABASE_URL, TEST_DATABASE_URL) |
| `app/db.py` | Engine, `SessionLocal`, `Base` (DeclarativeBase), `get_db` |
| `app/commercial/models.py` | `Lead` |
| `app/catalog/models.py` | `Service` |
| `app/organization/models.py` | `Location`, `Practitioner`, `PractitionerCapability` |
| `app/scheduling/models.py` | `AvailabilityRule`, `ScheduleBlock`, `Appointment` (with `ExcludeConstraint`) |
| `app/audit/models.py` | `AuditEvent` |
| `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_lead_to_appointment.py` | Migration machinery + 0001 |
| `tests/conftest.py` | Creates `odontoflow_test` if missing; reset+upgrade per session; truncation per test; `session` fixture |
| `tests/test_migrations.py` | Upgrade-from-empty, schema/constraints existence, downgrade, re-upgrade |
| `tests/test_schema_constraints.py` | FK + CHECK rejections |
| `tests/test_booking_invariant.py` | Non-overlap allowed; overlap rejected (23P01); cancelled-release semantics |
| `tests/test_health.py` | Task 1 `/health` regression (unchanged) |

## Test / migration evidence

- `pytest -q` → **15 passed** (12 new + 3 invariant) on real PostgreSQL 15 (`odontoflow_test` on 127.0.0.1:5434).
- CLI on clean DB: `alembic upgrade head` → `0001 (head)`; `downgrade base` → empty public schema + empty `alembic_version`; `upgrade head` again → `0001 (head)`. All PASS.
- Direct SQL: overlapping confirmed insert → `ERROR: conflicting key value violates exclusion constraint "excl_appointments_confirmed_no_overlap"` (23P01).
- `tests/test_health.py` → 1 passed (`/health` regression).

## Decisions made

- **Port 5434** for OdontoFlow Postgres (5432/5433 occupied by other projects; Contralatam container untouched).
- **Integer IDENTITY PKs** (MediStock SERIAL precedent, adapted to modern SQLAlchemy).
- **CHECK constraints instead of PG enum types** (simpler downgrade; state sets are small and stable).
- **`state='confirmed'` partial predicate** on the exclusion — cancelled appointments do not consume availability (spec rule).
- **`service_need_id` on `leads`** as nullable FK to `services` (spec: "associate the lead with an active service need"; leads may arrive before the service is decided).
- **`commercial_status` default `'new'`** (spec defers the full progression).
- **`from __future__ import annotations`** in model modules (forward references across modules).
- **Alembic URL resolution**: explicit config URL wins; env fallback otherwise (fixes test vs dev DB separation).
- **`odontoflow_test` created on demand** by conftest against the OdontoFlow container only.

## Blockers / risks

- `odontoflow_test` is created/dropped-schema by tests; do not point `TEST_DATABASE_URL` at a shared server.
- `ExcludeConstraint` is exported as `sqlalchemy.dialects.postgresql.ExcludeConstraint` (not top-level `sa`) — future code must import from the dialect.
- No availability/slot/booking logic yet — the GiST constraint is the safety net; preflight validation comes in later tasks.
- pytest emits Starlette/httpx deprecation warnings (non-blocking).

## Next recommended task

**Task 3 — Error contract**: `app/errors.py` with stable `ErrorCode` enum and FastAPI exception handlers (422/404/409) mapping `23P01` → `APPOINTMENT_CONFLICT` 409, with unit tests. Do not start booking services.
