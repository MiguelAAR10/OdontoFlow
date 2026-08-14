# PF3 — ExecutionContext & Audit Provenance (handoff)

## ExecutionContext contract

`ExecutionContext` (value object, `app/iam/context.py`) carries exactly the PF0 §13 fields:

- `organization_id` — acting tenant;
- `principal_id` — the acting Principal (int);
- `principal_type` — `human | agent | integration | system` (read from the `principals` row, never from headers/body — F-9);
- `request_id` — unique per transport invocation (uuid4 hex), generated, never read from a header (PF0 §13 X3);
- `correlation_id` — `X-Correlation-Id` header when present, else derived from `request_id` (X5: never NULL again).

Contract rule: application services receive the RESOLVED context explicitly (parameter `ctx: ExecutionContext | None = None`); no ContextVar as the primary contract.

## HTTP propagation

`app/context.py` is the single transport adapter:

- `new_request_id()` — per-request uuid4 hex;
- `default_context(organization_id=None)` — trusted/default context: seeded `system` principal + bootstrap organization (compatibilidad pre-auth; mantiene verdes las fixtures);
- `resolve_http_context(request)` — called in the booking/cancel/reschedule routers (`app/scheduling/router.py`), derives context per request, honors `X-Correlation-Id`.

PF3 is NOT login: identity binding is the trusted/default one (BLOCKER-1 option b per PF0). Authentication later replaces only this seam.

## Authorization wiring

- Reuses PF2 exactly: `require_permission(...)` from `app/iam/service.py` with machine-readable codes from `app/iam/permissions.py` (`appointments.create`, `appointments.reschedule`, `appointments.cancel`).
- The authoritative permission check is the FIRST statement inside the booking transaction (E6/F-19); the tenant comes from the context, never from a body field (X3).
- No hardcoded role names; no duplicated IAM logic.

## AuditEvent provenance

`record_event` (`app/audit/service.py`) now accepts `ctx`:

- `organization_id` ← `ctx.organization_id`;
- `actor_id` ← `str(ctx.principal_id)`;
- `actor_type` ← `ctx.principal_type`;
- `correlation_id` ← `ctx.correlation_id`.

Legacy keyword form survives for pre-principal callers (e.g., `organization.created` self-reference audit, D7). Tenant attribution still written at event time (F-17).

## Transaction preservation

- Audit rows are staged by `record_event` inside the caller's open transaction; booking/cancel/reschedule still own `with session.begin()`; audit commits atomically with the mutation. No BackgroundTasks.

## Compatibility behavior

- `app/scheduling/service.py` `_resolved_context()`: explicit `ctx` wins; otherwise trusted/default context (system principal + acting org, bootstrap fallback) — exactly the PF1 tenancy seam contract (`app/tenancy.py` docstring), now satisfied by PF3.
- Existing 258 tests remain green untouched in intent; only booking/cancel/reschedule tests were adapted to pass `ctx` explicitly where they assert provenance.

## Tests / full suite

`tests/test_execution_context.py` (16 tests, real PostgreSQL): all required cases 1-15 plus default-context:

- context carries all fields; request_id unique per request; correlation propagation (supplied + derived);
- Human and Agent authorized actions record their respective principal provenance through the SAME business path (identical behavior, different auditable actor);
- organization attribution from context; cross-org context cannot mutate another tenant;
- location-scoped permission enforced; inactive membership denied;
- booking/reschedule/cancel audit stores provenance;
- failed mutation → zero audit rows; mutation + audit atomic (rollback → no audit).

Full suite: **274 passed** (258 prior + 16 new).

## Changed files

- `app/context.py` (NEW) — transport adapter + default context;
- `app/iam/context.py` (already present from PF2 spec scope — verified) — ExecutionContext type;
- `app/audit/service.py` — `record_event` ctx-based provenance;
- `app/scheduling/service.py` — `ctx` on book/cancel/reschedule, `require_permission` inside transaction, tenancy seam replaced;
- `app/scheduling/router.py` — `resolve_http_context` wiring + `X-Correlation-Id` passthrough;
- `tests/test_execution_context.py` (NEW) + adaptations in `test_booking.py`, `test_cancellation.py`, `test_rescheduling.py`.

Forbidden surfaces untouched: `app/errors.py`, `app/db.py`, `app/scheduling/availability.py`, `app/iam/models.py`, migrations, `../medistock`.

## Blockers / risks

- No authentication exists: the resolved principal is always the trusted `system` actor via HTTP until login lands (PF0 BLOCKER-1). This is by design (PF3 ≠ login).
- `organization_id` fallback in services remains for pre-context callers; remove once all transports pass `ctx`.

## PF3: CLOSED
