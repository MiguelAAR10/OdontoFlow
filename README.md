# OdontoFlow

**A multi-tenant, agent-native clinic operations platform.**

OdontoFlow is a deterministic, PostgreSQL-first operational ERP for dental clinics, built as the successor of
the legacy `MediStock` Flask backend (a read-only reference, not a rewrite target — see
[`docs/backend-evolution.md`](docs/backend-evolution.md)). It turns a commercial **Lead** into a **confirmed
appointment** through typed FastAPI contracts, transactional booking with database-enforced conflict safety,
and an audit trail that records who did what and why — so that humans, agents, and integrations can operate
the same domain layer under the same rules.

> **Status:** Vertical 1 (Lead-to-Appointment) **CLOSED** · Platform Foundation **PF1–PF4 CLOSED**
> (tenant integrity, authorization, execution context/audit, idempotent commands) · Frontend integration
> **contract defined, first vertical pending**. See [`docs/roadmap.md`](docs/roadmap.md) and
> [`docs/integration/`](docs/integration/).

---

## Why OdontoFlow

- **Deterministic by design.** Service duration, availability, eligibility, and conflicts are authoritative in
  code + PostgreSQL — never in an LLM. An LLM may suggest; it never decides prices, slots, or bookings.
- **Database is the final authority.** A partial GiST exclusion constraint makes overlapping confirmed
  appointments for the same practitioner *physically impossible*, even under a two-request race.
- **Multi-tenant foundation.** `Organization` is the tenant root; every tenant-consistency relationship is
  enforced by a PostgreSQL composite foreign key, so cross-tenant states are structurally impossible, not
  just "validated" in application code.
- **Agent-native by design, not yet by wiring.** A permission-based IAM (`Principal` = human | agent |
  integration | system) and an explicit `ExecutionContext` exist so that future agents call the exact same
  deterministic services humans use, with auditable provenance — but no authentication exists yet, and
  context/permission enforcement currently covers booking, cancellation, and rescheduling only. See
  [`docs/architecture.md`](docs/architecture.md) §9 for the full, current gap list.

## Architecture, in one picture

One FastAPI deployment, six explicit module boundaries under `app/` (`commercial`, `catalog`, `organization`,
`scheduling`, `iam`, `audit`), no message queue, no LLM library anywhere in `app/`. Full detail, module
responsibilities, and the invariants PostgreSQL enforces: [`docs/architecture.md`](docs/architecture.md).

```
Caller (HTTP today; future agent tool)
  → FastAPI router (thin: HTTP shape → schema → service)
    → ExecutionContext (explicit: org, principal, request_id, correlation_id)
      → Application service (owns its transaction; permission check first)
        → PostgreSQL (composite tenant FKs + partial GiST exclusion + CHECKs)
        → AuditEvent (same transaction — atomic with the mutation)
```

## Current development status

- **Vertical 1 — Lead to Appointment: CLOSED.** Full commercial-to-booking journey, proven end-to-end over
  HTTP against real PostgreSQL.
- **Platform Foundation PF1–PF4: CLOSED.** Tenant integrity (composite FKs), permission-based IAM,
  execution provenance, and durable command idempotency (`command_receipts`, no Redis) exist and are
  tested (294 tests). Authentication does not exist yet — identity today is the trusted default
  (`system` principal, bootstrap org) per PF3; read [`docs/architecture.md`](docs/architecture.md) §9
  before assuming more.
- **Frontend integration: contract defined.** The OdontoSmart frontend (sibling repository) was inspected
  read-only; the evidence-backed integration contract, the action→endpoint matrix and the first vertical
  (Agenda ↔ Scheduling) live in [`docs/integration/`](docs/integration/). No integration code is merged yet.

## Tech stack

| Layer | Choice |
|---|---|
| API | FastAPI (sync routes, Pydantic v2, auto OpenAPI at `/openapi.json`) |
| ORM | SQLAlchemy 2.0 (declarative, typed `Mapped[...]`) |
| DB | PostgreSQL 15 (`btree_gist`, JSONB, partial GiST exclusion) |
| Migrations | Alembic (`0001` → `0002` → `0003` → `0004`) |
| Tests | pytest + real PostgreSQL (`odontoflow_test`) |
| Runtime | Docker Compose (Postgres), Python 3.12 |

No Redis. No Kafka. No async migration. No LLM libraries. The frontend lives in a sibling repository
(`../odontoflow-frontend`); this repo is the domain authority and ships no UI code.

---

## Repository layout

```
app/
  __init__.py            # create_app(): FastAPI factory, routers, error handlers
  config.py              # env-driven settings
  db.py                  # engine, SessionLocal, Base, get_db
  errors.py              # stable error envelope + handlers (23P01 → 409)
  context.py             # ExecutionContext transport adapter (PF3)
  tenancy.py             # bootstrap organization seam (PF1)
  audit/                 # AuditEvent model + record_event
  catalog/               # Service (models/schemas/service)
  commercial/            # Lead (models/schemas/service)
  organization/          # Organization, Location, Practitioner, Capability, Membership
  scheduling/            # AvailabilityRule, ScheduleBlock, Appointment,
                         # availability.py (pure slot engine), query.py, service.py
  iam/                   # Principal, Membership, Role, Permission, RoleAssignment,
                         # context.py (ExecutionContext), permissions.py, service.py
  idempotency/           # CommandReceipt + run_idempotent_command (PF4)
alembic/versions/        # 0001 vertical · 0002 org/tenant · 0003 iam · 0004 command_receipts
docs/superpowers/
  specs/                 # approved design specs (Vertical 1, Platform Foundation)
  evidence/              # platform readiness audits
  handoffs/              # per-task reports (Tasks 1-10, PF1-PF4)
docs/integration/        # frontend ↔ backend contract, matrix, data flows, first vertical
tests/                   # 294 tests: unit + integration against real PostgreSQL
```

---

## Domain model, API reference, error contract, invariants & audit

Moved to [`docs/architecture.md`](docs/architecture.md) to keep this file scannable in five minutes.
Quick pointers: the full HTTP contract is generated at `docs/api/openapi.yaml` / `openapi.json` and served
live at `/docs`; every error uses one stable envelope, `{"error": {"code", "message", "details"}}`; the
practitioner-overlap invariant is a partial GiST exclusion constraint PostgreSQL enforces, not application
code.

---

## Running locally

Requirements: Docker (Compose), Python 3.12, git.

```bash
# 1. PostgreSQL (host port 5434 — keeps other projects untouched)
docker compose up -d db

# 2. Environment
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # adjust DATABASE_URL if needed

# 3. Migrations
.venv/bin/alembic upgrade head

# 4. API
.venv/bin/uvicorn app:app --reload --port 8000
# → http://127.0.0.1:8000/docs   ·   http://127.0.0.1:8000/openapi.json
```

> The test database `odontoflow_test` is created automatically by the test suite on port 5434. Ports 5432/5433 belong to other projects and are never touched.

---

## Testing

```bash
# Full suite (real PostgreSQL — no SQLite, no mocks for DB invariants)
.venv/bin/python -m pytest -q        # 294 tests

# Focused
.venv/bin/python -m pytest tests/test_idempotency.py tests/test_booking_invariant.py -q
```

The suite covers: migrations upgrade/downgrade cycles, GiST overlap rejection with real races (two sessions + threads + `Barrier`, no sleeps), tenant-integrity proofs (cross-org states rejected by the DB), authorization (org-wide vs location scopes, inactive memberships), execution-context provenance, PF4 idempotency (exactly-once, replay, fingerprint mismatch, rollback, concurrency), and a full HTTP E2E journey (lead → catalog → organization → availability → slot → book → reschedule → cancel) with audit verification.

---

## Roadmap

- **DONE** — Lead → Appointment (Vertical 1, closed); multi-tenant foundation (PF1); authorization (PF2);
  execution provenance (PF3); idempotent commands (PF4).
- **NOW** — Frontend integration, first vertical **Agenda ↔ Scheduling** (read endpoints + frontend adapter;
  contract in [`docs/integration/frontend-backend-contract.md`](docs/integration/frontend-backend-contract.md)).
- **NEXT** — Platform Foundation gap closure; Clinical Bridge (`Appointment → Patient → Visit → ServiceExecution`).
- **LATER** — Finance, Inventory/Operations, external adapters, operational optimization, agent execution.

Full detail: [`docs/roadmap.md`](docs/roadmap.md).

---

## Documentation

Start at [`docs/README.md`](docs/README.md) — it explains the difference between curated docs (living,
rewritten as the system changes) and the `docs/superpowers/` engineering record (specs, plans, evidence,
handoffs — append-only, one authoritative snapshot per file).

| Doc | Answers |
|---|---|
| [`docs/product-vision.md`](docs/product-vision.md) | What is OdontoFlow, and where is it going (clearly marked FUTURE)? |
| [`docs/architecture.md`](docs/architecture.md) | How does the system implemented today actually work, including known gaps? |
| [`docs/backend-platform-blueprint.md`](docs/backend-platform-blueprint.md) | Detailed technical authority: principles + why, full vertical lifecycle, PF1–PF4 rationale, MediStock migration map |
| [`docs/backend-evolution.md`](docs/backend-evolution.md) | How did the backend get here, commit by commit? |
| [`docs/roadmap.md`](docs/roadmap.md) | DONE / NOW / NEXT / LATER |
| [`docs/integration/frontend-current-state.md`](docs/integration/frontend-current-state.md) | Current state of the OdontoSmart frontend (read-only inspection) |
| [`docs/integration/frontend-backend-contract.md`](docs/integration/frontend-backend-contract.md) | Action→endpoint matrix + first vertical definition (Agenda ↔ Scheduling) |
| [`docs/integration/module-integration-map.md`](docs/integration/module-integration-map.md) | Frontend module → backend module mapping |
| [`docs/integration/data-flow.md`](docs/integration/data-flow.md) | Read/booking/idempotency/error flows across the boundary |
| `docs/superpowers/specs/2026-08-12-lead-to-appointment-design.md` | Approved Vertical 1 design |
| `docs/superpowers/specs/2026-08-14-platform-foundation-design.md` | Approved PF0 platform design (tenant model, IAM, context, idempotency, PF1–PF4) |
| `docs/superpowers/evidence/*` | Platform readiness audits (data ownership, actors/commands) |
| `docs/superpowers/handoffs/*` | Per-task evidence reports (Tasks 1–10, PF1–PF4) |

---

*OdontoFlow — deterministic clinic operations, agent-native by design. MediStock (legacy Flask reference) remains read-only and untouched.*
