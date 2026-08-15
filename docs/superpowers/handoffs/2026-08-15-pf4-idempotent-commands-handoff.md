# PF4 — Idempotent Commands (handoff)

Date: 2026-08-15 · Authority: `docs/superpowers/specs/2026-08-14-platform-foundation-design.md` §15–§16, PF4 section
Base: HEAD `1a737b0` (app code; repo head `8dd83eb` adds docs only) · Suite before: 274 passed

## What was built

Durable exactly-once execution for `appointments.book`, `appointments.reschedule`
and `appointments.cancel` using a PostgreSQL `command_receipts` table (migration
`0004`). The unique index `uq_command_receipts_org_operation_key` is the whole
concurrency mechanism: no PENDING state machine, no polling, no advisory locks,
no sleeps, no Redis, no middleware, no async.

## Contract mechanics (as implemented)

- **Claim first (§16.1).** `_claim_receipt` (`app/scheduling/service.py`) stages
  and flushes the receipt row as the first statement of the command's own
  `with session.begin()` — before `require_permission` and before the
  `FOR UPDATE` row lock in cancel/reschedule. A duplicate key surfaces as
  `23505` on the receipt constraint before any preflight work, and no command
  ever holds a GiST or row lock while waiting on the receipt index.
- **Execute + settle.** The existing services keep owning their transaction
  (A2/C9). On success the service fills `resource_type`, `resource_id` and
  `outcome_json` (`_settle_receipt`) so receipt, mutation and audit row commit
  together or not at all (I5/I13). A rolled-back command leaves no receipt
  (I7/C3) — a later retry re-executes.
- **Collision resolution (§16.2).** `app/idempotency/service.py` —
  `run_idempotent_command` catches `23505` only when
  `diag.constraint_name == uq_command_receipts_org_operation_key` (C7),
  rolls back, and reads the committed receipt in a **separate** transaction
  (C9). Matching fingerprint AND matching principal → REPLAY of the stored
  logical outcome (C1/C4); anything else → `IDEMPOTENCY_KEY_REUSED` (409,
  `details={}`) (C2/C6/I8).
- **Transport (C10).** `app/scheduling/router.py` reads the optional
  `Idempotency-Key` header and passes it to the handler; on replay it renders
  the stored outcome into `AppointmentRead` (I5) and sets the optional
  non-authoritative `Idempotent-Replay: true` header (I9). The booking
  `40P01` one-shot retry wraps the whole idempotent command, so a deadlock
  rolls the claim back with the attempt and the retry re-claims (C8).
- **Key policy (I10/I11).** Agent/integration principals without a key →
  `INVALID_INPUT` (422) before any mutation; absent key = today's behaviour,
  no receipt.
- **Errors.** One new code `IDEMPOTENCY_KEY_REUSED` (409) in `app/errors.py`;
  no blanket `23505` mapping (C7).

## Required proofs (real PostgreSQL, threads + `Barrier`, no sleeps)

`tests/test_idempotency.py` — 20 tests, all passing:

| Proof | Test |
|---|---|
| Concurrent same-key booking → exactly one appointment/audit/receipt, both callers same outcome (C1) | `test_concurrent_same_key_booking_executes_exactly_once` |
| Concurrent same-key reschedule → exactly one (C1) | `test_concurrent_same_key_reschedule_executes_exactly_once` |
| Sequential booking replay → stored outcome, no 409, one row each (C4) | `test_sequential_booking_replay_returns_stored_outcome` |
| Booking replay over HTTP → same 201 body + `Idempotent-Replay: true` (I9) | `test_booking_replay_via_http_returns_original_outcome_and_replay_header` |
| Cancel replay → stored outcome, no duplicate audit (C4) | `test_cancel_replay_returns_stored_outcome_without_duplicate_audit` |
| Reschedule replay → stored outcome, no `before == after` audit (C4) | `test_reschedule_replay_returns_stored_outcome_without_duplicate_audit` |
| Same key + different fingerprint → `IDEMPOTENCY_KEY_REUSED`, zero new rows (C2) | `test_same_key_different_fingerprint_rejected_without_mutation` (+ HTTP 409 test) |
| Rollback (SLOT_BLOCKED) leaves no receipt; retry executes (C3) | `test_rolled_back_command_leaves_no_receipt_then_retry_executes` |
| Different keys, same slot → GiST unchanged; loser's receipt vanishes (C5) | `test_different_keys_same_slot_sequential_is_slot_blocked`, `test_concurrent_different_keys_same_slot_settle_by_gist` |
| Cross-principal replay refused (C6) | `test_cross_principal_replay_is_refused` |
| Authorization not bypassed by keyed command | `test_keyed_command_still_enforces_authorization` |
| Tenant isolation: same key in two orgs executes independently (I2) | `test_same_key_in_two_organizations_executes_independently` |
| Agent without key → `INVALID_INPUT` before mutation (I10) | `test_agent_principal_without_key_rejected_before_mutation` |
| Absent key → today's behaviour, no receipt (I11) | `test_absent_key_keeps_today_behaviour_and_writes_no_receipt` |
| `40P01` retry re-claims cleanly (C8) | `test_40p01_retry_reclaims_cleanly` |
| Non-receipt `23505` not treated as idempotency (C7) | `test_non_receipt_23505_is_not_an_idempotency_event` |
| Session stays idle; replay in separate transaction (C9) | `test_replay_and_execute_leave_session_idle` |
| Fingerprint canonical (tz-normalized, excludes transport noise) (I4) | `test_fingerprint_is_canonical_and_excludes_transport_noise` |

## Test runs

- Focused: `tests/test_idempotency.py` → **20 passed** (3× consecutive runs, no flake).
- Full suite: `.venv/bin/python -m pytest -q` → **294 passed** (274 prior + 20 new).
- Adapted pre-existing tests (expected-set drift only): `tests/test_migrations.py`
  (`EXPECTED_TABLES` + `HEAD_REVISION = "0004"`), `tests/test_tenant_integrity.py`
  (adds `command_receipts` to the tenant-owned NOT-NULL list), `tests/conftest.py`
  (truncate `command_receipts`).

## Independent review (ONE, read-only, DeepSeek V4 Flash via the validated runner)

- Request: `opencode-go/deepseek-v4-flash` through
  `medistock/.audit/herdr-contract/tooling/runner/runner.mjs` (deny-by-default
  scout: read/glob/grep only; 17 read/grep/glob calls, all completed).
- Verdict: **PASS** — zero BLOCKERs, zero ISSUEs; all 11 contract clauses
  verified with `file:line` evidence. Reviewer notes (non-findings):
  claim-first ordering satisfied; replay read separate/non-nested;
  non-member keyed command → `23503` before permission is unreachable via the
  current single transport.
- First two review attempts failed with a gateway `fetch failed` on a
  long-generation prompt (no receipt persisted — provider failures are never
  memoized, I7); the tighter review prompt succeeded. No repair was needed.

## Forbidden surfaces (untouched)

`app/db.py` · `app/scheduling/availability.py` · `app/iam/*` · `app/audit/*` ·
existing migrations · `../medistock`. The practitioner-global GiST exclusion
and the `23P01`/`40P01` behaviour are unchanged (C5/C7/C8 tests).

## Changed / new files

- `alembic/versions/0004_command_receipts.py` (NEW)
- `app/idempotency/__init__.py`, `models.py`, `service.py` (NEW)
- `tests/test_idempotency.py` (NEW)
- `app/errors.py` — `IDEMPOTENCY_KEY_REUSED` (409)
- `app/scheduling/service.py` — `idempotency` claim/settle on book/cancel/reschedule
- `app/scheduling/router.py` — `Idempotency-Key` read, handler wiring, `Idempotent-Replay` header
- `tests/conftest.py`, `tests/test_migrations.py`, `tests/test_tenant_integrity.py` — table/revision drift

## Risks / notes

- `opencode agent list` rendering quirks are unrelated to this block.
- The replay transaction is not SQL-`READ ONLY`; it performs a single SELECT
  and never writes (C9 holds).
- `command_receipts` rows are append-only after commit; no retention policy
  exists yet (out of scope — no reaper by design, §16).

## PF4: CLOSED
