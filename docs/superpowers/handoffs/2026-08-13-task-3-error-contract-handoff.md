# Task 3 Handoff — Stable API Error Contract

**Commit:** see resulting commit SHA below
**Parent:** `ce4757f` (Task 2 handoff)
**Repo:** `/home/miguel/projects/portfolio/AI-EdgeRunners/OdontoFlow`
**Date:** 2026-08-13

## Objective

Establish the stable application error contract: closed `ErrorCode` enum, `AppError` abstraction, consistent JSON error envelope, and FastAPI handlers for `AppError`, request validation, and SQLAlchemy `IntegrityError` (deterministic translation only for PostgreSQL exclusion violation `23P01` → `APPOINTMENT_CONFLICT`).

## Files changed and why

| File | Change | Why |
|---|---|---|
| `app/errors.py` | NEW | `ErrorCode`, `AppError`, `register_error_handlers`, `_sqlstate` extraction, payload builder |
| `app/__init__.py` | MODIFIED | `create_app()` calls `register_error_handlers(app)` — the only application wiring change |
| `tests/test_errors.py` | NEW | TDD verification: 7 deterministic cases on a test-only FastAPI app (kept under `tests/`) |

No routers, no services, no business entities were added. Migration `0001` untouched. MediStock untouched.

## Public error contract

Response envelope (always JSON):

```json
{
  "error": {
    "code": "APPOINTMENT_CONFLICT",
    "message": "The requested appointment slot is no longer available.",
    "details": {}
  }
}
```

Never exposed: SQL, constraint names, SQLSTATE values, stack traces, database internals, raw exception messages.

## ErrorCode values and HTTP mappings

| ErrorCode | HTTP |
|---|---|
| `INVALID_INPUT` | 422 |
| `NOT_FOUND` | 404 |
| `ENTITY_INACTIVE` | 409 |
| `CAPABILITY_MISSING` | 409 |
| `SLOT_BLOCKED` | 409 |
| `APPOINTMENT_CONFLICT` | 409 |

`AppError(code, message=None, *, details=None, http_status=None)` — message and status default from the code; details always a dict.

## How 23P01 is detected

`IntegrityError` handler extracts the SQLSTATE from the **DBAPI exception object**, never from message text:

- `exc.orig` is the psycopg v3 exception.
- Priority: `orig.sqlstate` → `orig.pgcode` → `orig.diag.sqlstate`.
- `== "23P01"` → `409 APPOINTMENT_CONFLICT` with the safe message.
- Any other SQLSTATE → the handler **re-raises**; the exception propagates to FastAPI's generic 500 ("Internal Server Error") — never mislabeled as `APPOINTMENT_CONFLICT`, never leaking DB text.

## Tests executed and results

`python -m pytest -q` → **22 passed** (15 prior + 7 new):

1. `AppError NOT_FOUND` → 404 envelope, code `NOT_FOUND`, empty details.
2. `AppError ENTITY_INACTIVE` → 409 envelope, code `ENTITY_INACTIVE`.
3. `AppError APPOINTMENT_CONFLICT` → 409 envelope with safe message.
4. Validation error → 422 `INVALID_INPUT`, no `detail`/`loc`/traceback leakage.
5. Real PostgreSQL overlapping insert (via test route) → 409 `APPOINTMENT_CONFLICT`; response body contains no constraint name, no "conflicting key", no `23P01`.
6. Unknown `IntegrityError` (duplicate service name, 23505) → 500; body is not `APPOINTMENT_CONFLICT` and contains no DB text.
7. `/health` unchanged → 200 `{"status": "ok"}`.

## Confirmation

- Task 2 invariants still pass: all prior 15 tests green (migrations, constraints, GiST overlap rejection, health).
- MediStock untouched: `git -C ../medistock status` clean at `ef2fffb7a348aa621f7a5b387e09a1553351000f`.

## Decisions

- **Re-raise, don't translate**, unknown `IntegrityError` → generic 500, no mislabeling, no leak (keeps the enum closed to the six approved codes).
- Validation errors return the standard envelope with the safe message; no field-level internals are forwarded.
- Handlers registered via a single `register_error_handlers(app)` called from `create_app()` so production wiring is exactly what tests exercise.
- Test-only endpoints live under `tests/test_errors.py` on top of `create_app()`.

## Blockers

None.

## Risks

- Future code must raise `AppError` with a specific `ErrorCode` for 404/409 domain semantics; raising raw exceptions will yield generic 500s.
- The `IntegrityError` handler relies on psycopg v3 attribute shape (`sqlstate`/`pgcode`); covered by an integration test so a driver upgrade would fail loudly.
- `SLOT_BLOCKED`/`CAPABILITY_MISSING`/`ENTITY_INACTIVE` are defined but unused until Tasks 4-9 — intentional.

## Recommended Task 4

**Catalog + Organization slice** (per plan): `app/catalog/*` + `app/organization/*` models/schemas/services with TDD (Service with authoritative duration, Location with IANA tz, Practitioner, PractitionerCapability, eligibility query), using `AppError` (`NOT_FOUND`, `ENTITY_INACTIVE`, `CAPABILITY_MISSING`) and the new envelope.
