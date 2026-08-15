# AGENTS.md — OdontoFlow Backend Engineering Directives

> **Read this before touching any code in this repository.**
> This document is the operating contract for every agent (human or AI) that develops OdontoFlow. It is written to be followed, not to inspire.

---

## 1. What OdontoFlow is (and is not)

OdontoFlow is a **deterministic, multi-tenant, agent-native clinic operations platform** (FastAPI + PostgreSQL, modular monolith). It is the successor of the legacy `MediStock` Flask backend.

**It is:**
- A transactional backend where PostgreSQL enforces the invariants (composite tenant FKs, partial GiST exclusion, CHECKs).
- A permission-based, multi-tenant ERP in construction (Organization is the tenant root).
- A platform where humans, agents and integrations call the **same** deterministic application/domain services, with auditable provenance.

**It is not:**
- A "MediStock rewrite in FastAPI". The Flask codebase is read-only behavioral reference only.
- An LLM-driven system. LLMs never set prices, durations, slots or bookings.
- A microservices / event-driven system. No Redis, no Kafka, no outbox, no async migration — by design.

---

## 2. Product vision (what we are building toward)

```
CRM (Lead) → Scheduling (Vertical 1, CLOSED) → Clinical Bridge (Patient/Visit/ServiceExecution)
    → Finance (Charge/Payment) → Inventory/Operations (Product/Consumption/Movement)
    → Agents as deterministic tools → external adapters (Calendar/WhatsApp/NubeFact)
```

The vision: **one deterministic domain layer** that any actor drives through typed contracts. Agents reason over state → constraints → candidate actions → deterministic tools → outcome → updated state. The optimization/world-model layer is deliberately **not** designed yet.

---

## 3. Non-negotiable architecture invariants

Every change must respect these. Violating them = wrong, even if tests pass.

1. **PostgreSQL is the final authority.** Overlap conflicts, tenant integrity, and value-domain rules are enforced by constraints, not by application checks alone.
2. **Duration is catalog-authoritative.** `Service.duration_minutes` is the only source; clients can never supply duration/end/state (schemas use `extra="forbid"`).
3. **Slots are pure logic.** `app/scheduling/availability.py` is stdlib-only, half-open `[start, end)`, 15-minute grid in the location's IANA timezone. Never import DB/FastAPI there.
4. **Transaction ownership lives in services.** `book/cancel/reschedule` call `session.begin()` **before** any read; routers never open transactions and never run pre-transaction queries.
5. **Audit is atomic with the mutation.** `record_event` stages the row inside the caller's transaction. No BackgroundTasks, no separate commits.
6. **Tenant integrity is structural.** Cross-organization relational states must be **impossible at the DB level** (composite FKs into `UNIQUE(organization_id, id)`), never "just validated".
7. **The practitioner-global GiST stays.** `EXCLUDE (practitioner_id =, tstzrange &&) WHERE state='confirmed'` — a practitioner cannot be double-booked across organizations. Never add organization to that key.
8. **Explicit context, no magic.** `ExecutionContext` (organization, principal, request_id, correlation_id) is an explicit parameter at service boundaries. No ContextVar as the primary contract.
9. **Authorization is permission-based.** Machine-readable codes (`appointments.create`, …) via `RolePermission`. No `if role == "owner"` anywhere.
10. **Stable error envelope.** `{ "error": { "code", "message", "details" } }` with the six approved codes. Never leak SQL, constraint names, SQLSTATE or stack traces.

---

## 4. How work is developed here (the methodology)

Every task follows this exact loop. No exceptions.

```
pre-flight  →  tree clean · full suite green · MediStock clean
   ↓
TDD         →  write the failing tests FIRST (real PostgreSQL)
   ↓
implement   →  smallest change that satisfies the approved spec
   ↓
fan-in      →  focused tests → full suite → diff of allowed surfaces only
   ↓
review      →  independent reviewer checks the diff against the task contract
   ↓
commit      →  ONE commit per task, implementation + tests + handoff
```

**Commit rules**
- One task = one commit. Message format: `feat|fix|test|docs: <summary>`.
- Every implementation task lands with its handoff under `docs/superpowers/handoffs/`.
- `main` must stay green: `.venv/bin/python -m pytest -q` → 0 failures before and after.

**Scope guard** — do NOT touch unless the task explicitly says so:
`app/errors.py` (error contract) · `app/db.py` (session lifecycle) · `app/scheduling/availability.py` (pure engine) · existing migrations · `../medistock` (read-only).

---

## 5. Where things live

| Area | Path | Contract |
|---|---|---|
| FastAPI factory | `app/__init__.py` | `create_app()` — only place routers/handlers are registered |
| Session/DB | `app/db.py` | `engine`, `SessionLocal`, `Base`, `get_db` |
| Error contract | `app/errors.py` | `ErrorCode`, `AppError`, `register_error_handlers` |
| Context adapter | `app/context.py` | `resolve_http_context`, `default_context` |
| Tenancy seam | `app/tenancy.py` | bootstrap org resolution (replaced by ctx in PF3) |
| Commercial | `app/commercial/` | Lead models/schemas/service/router |
| Catalog | `app/catalog/` | Service models/schemas/service/router |
| Organization | `app/organization/` | Organization, Location, Practitioner, Capability, Membership |
| Scheduling | `app/scheduling/` | models, `availability.py` (pure), `query.py`, `service.py`, router |
| Audit | `app/audit/` | `AuditEvent`, `record_event` |
| IAM | `app/iam/` | Principal, Membership, Role, Permission, RoleAssignment, `has_permission`/`require_permission`, `context.py` |
| Migrations | `alembic/versions/` | 0001 vertical · 0002 tenant · 0003 iam |
| Tests | `tests/` | unit + integration against real PostgreSQL |
| Specs | `docs/superpowers/specs/` | approved design documents (authority) |
| Handoffs | `docs/superpowers/handoffs/` | per-task evidence reports |
| API docs | `docs/api/` | `openapi.yaml` / `openapi.json` (generated from `app:app`) |

---

## 6. Backend engineering rules (senior checklist)

**Data & persistence**
- Migrations are additive, reversible, and never recreate tables. New NOT NULL columns on existing tables get a staged backfill.
- All FKs use `ondelete="RESTRICT"`. All PKs are integer `Identity()` (the integer spine stays; public UUIDs are optional/additive only).
- Tenant-consistent composite FKs into `UNIQUE(organization_id, id)` for any row carrying `organization_id + location_id/service_id/lead_id`.
- Timestamps are `DateTime(timezone=True)`; instants persisted in UTC; local wall-clock only where the spec demands (availability rules).

**Services**
- Accept an explicit SQLAlchemy `Session`. Pure logic stays pure (no Session in `availability.py`).
- Pre-validation returns stable `AppError` codes; the DB constraint is the final authority for races.
- After an `IntegrityError`, `session.rollback()` before reusing the session.

**API**
- Routers are thin: HTTP shape → Pydantic schema → service → typed response. No business logic in routers.
- Every response has a declared `response_model`. No raw ORM returns.
- Creations return `201`; reads `200`; errors use the single envelope.

**Tests**
- Real PostgreSQL (`odontoflow_test` on port 5434). No SQLite, no mocked DB invariants.
- Concurrency tests use two sessions + threads + deterministic synchronization (`Barrier`/`pg_locks`) — never `sleep()`.
- Never run two pytest processes concurrently: the test database is shared.

---

## 7. Agent operation rules (how AI agents work here)

- **Read `AGENTS.md` and the approved spec first.** Do not redesign approved specs.
- **TDD is mandatory.** Write the failing test before the implementation.
- **Allowed write surface only.** Each task defines which files may change; touching anything else is a defect.
- **No commit until fan-in.** The orchestrator verifies: allowed diff, full suite green, reviewer verdict, MediStock clean — then commits.
- **MediStock is read-only.** Never modify `../medistock`.
- **Never use `pkill`.** Terminate processes by exact PID only.
- **Never run pytest while another builder/reviewer runs it** (shared test DB).
- **Use headless `opencode run`** for OpenCode Go builders/reviewers; the OpenCode TUI is unstable inside Herdr (known Bun crash).
- **Update `CHANGELOG.md`** with every shipped change.

---

## 8. Roadmap & status

| Stage | Status |
|---|---|
| Vertical 1 — Lead to Appointment (Tasks 1–10) | **CLOSED** |
| PF0 — Platform foundation spec | **CLOSED** |
| PF1 — Organization & tenant integrity | **CLOSED** |
| PF2 — Principal & authorization | **CLOSED** |
| PF3 — ExecutionContext & audit provenance | **CLOSED** |
| PF4 — Idempotent commands (PostgreSQL CommandReceipt) | next |
| Clinical Bridge (Patient, Visit, ServiceExecution) | planned |
| Finance (Charge, Payment), Inventory/Operations (consumables, stock, sales) | planned — see `docs/MIGRATION.md` |
| Agents as deterministic tools · external adapters (Calendar/WhatsApp/NubeFact) | planned |

---

*This file is the single source of engineering directives for OdontoFlow. When in doubt, make the smaller change that preserves every invariant above.*
