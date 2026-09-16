# AGENT-CONFIRM-FAIL-CLOSED-03 — Technical handoff

Date: 2026-09-16
Base: `6b0d2226e930030c3c1582851c20b9894b56225d`
Status: **PASS — bounded fix verified; no next activity started**

## Result

The MVP fail-closed decision is implemented at the authoritative booking
command boundary. A server-resolved `agent` principal cannot confirm a pending
appointment proposal merely because a later inbound message exists. The
command returns stable `INVALID_INPUT`, leaves the proposal pending, creates
no appointment, and the tool gateway records the error audit event.

The guard does not inspect message text, accept LLM claims, accept caller
trust flags, or use contact-level communication consent. It runs after the
existing exact proposal lookup, confirmed replay path, expiry validation, and
temporal later-inbound guard. Authenticated human callers retain the existing
valid two-message confirmation path.

## Contract evidence

The inspected `Message` contract contains direction, body, timing, and
delivery metadata but no appointment-consent classification. `AppointmentProposal`
contains the exact proposed slot, conversation, token, status, and expiry but
no authoritative acceptance event or message reference. The confirmation tool
arguments contain only proposal ID and token. The cancellation flow’s explicit
source-message binding cannot be reused for booking because booking has no
approved source-message/consent contract.

`ExecutionContext.principal_type` is populated from the authenticated
PostgreSQL principal row. The Sales Agent credential is type `agent`; the
existing test/operator credential is type `human`. This is the only authority
signal used by the fix.

## Tests

Before the production change, the real-PostgreSQL adversarial regression
failed as intended:

```text
1 failed, 2 warnings
AssertionError: ('confirmed', 1, 0, 0) != ('proposed', 0, 1, 1)
```

After the change, focused tests passed:

```text
.venv/bin/python -m pytest -q -rs \
  tests/test_sales_agent_w4.py::test_wf01_three_turn_loop_persists_once_and_keeps_threads_isolated \
  tests/test_sales_agent_w4.py::test_wf01_rejects_same_turn_model_propose_then_confirm \
  tests/test_sales_agent_w4.py::test_wf01_rejects_negative_later_message_before_booking \
  tests/test_reception_agent_phase5.py::test_sales_agent_v0_cannot_confirm_or_use_dormant_permissions \
  tests/test_agent_booking_phase4.py::test_explicit_confirmation_books_once_and_returns_calendar_payload \
  tests/test_agent_booking_phase4.py::test_confirmation_is_bound_to_the_original_conversation \
  tests/test_agent_booking_phase4.py::test_expired_proposal_cannot_be_confirmed
7 passed, 2 warnings in 11.69s
```

Final serial full suite:

```text
.venv/bin/python -m pytest -q -rs
529 passed, 21 warnings in 505.22s (0:08:25)
```

Current collection is 529, compared with the historical 527-pass baseline.
`compileall`, `git diff --check`, and the protected-path check passed. Ruff
still reports the six import/unused-import diagnostics present at the base
commit; this change introduces no new ruff diagnostics.

## Files in the bounded change

- `app/agent_tools/booking.py` — fail-closed `agent` principal guard.
- `tests/test_sales_agent_w4.py` — retained negative real-PostgreSQL
  regression, isolated memory database fixture, and fail-closed W4
  expectations.
- `tests/test_reception_agent_phase5.py` — agent rejection plus authenticated
  human confirmation coverage; dormant permission checks remain.
- `CHANGELOG.md` — shipped MVP behavior and future verified-acceptance
  dependency.
- This handoff.

Pre-existing dirty/untracked paths were not staged or changed by this task.

## Invariants and limitations

The temporal guard remains intact. Tenant/conversation/token binding, proposal
expiry, idempotency/replay, audit, and exactly-one appointment creation remain
covered by the focused and full suites. No migration, schema, channel,
deployment, production data, or unauthenticated operator bypass was added.

Automatic agent booking is intentionally unavailable until a future product
decision defines a trusted, authenticated, proposal-bound patient acceptance
mechanism. The current raw free-text message representation is insufficient;
keywords and LLM interpretation remain non-authoritative.

## Commit

The implementation, tests, changelog, and this handoff are included in the
single bounded task commit. The final SHA is recorded in the planning CAVELOG
and STATUS records and in the task close-out report.

Planning living brief:
`../odontoflow-planning/docs/handoffs/plans/2026-09-16-agent-confirm-fail-closed-03.md`
