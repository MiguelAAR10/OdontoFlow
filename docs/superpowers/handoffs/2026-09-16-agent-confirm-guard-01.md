# AGENT-CONFIRM-GUARD-01 — Booking confirmation guard handoff

Date: 2026-09-16
Base: `b7f11ce` (`main`)
Status: **PASS** — bounded implementation and verification complete.

## Objective and boundary

Enforce the existing Patient Confirmation contract at the canonical booking
command: an Appointment may be created only after a persisted inbound message
from the same conversation is later than the existing appointment proposal.
This task does not interpret model text, add a schema or migration, connect a
channel, or change the remaining MVP-01 blockers.

## Implementation

`confirm_contact_booking_proposal` now queries PostgreSQL for an inbound
`Message` in the organization and proposal conversation whose persisted
`created_at` is later than `AppointmentProposal.created_at`. If none exists,
the command raises the stable `INVALID_INPUT` error before
`_book_appointment_core` runs. Proposal status/TTL, conversation/token binding,
idempotency, and the existing atomic audit path remain in place.

The ordering and failure semantics mirror the established cancellation guard.
The check is authoritative persisted conversation evidence; it does not trust
the model's claim that a patient confirmed. No protected path, migration, or
production database was touched.

## Evidence and validation

TDD red phase:

```text
$ .venv/bin/python -m pytest -q tests/test_sales_agent_w4.py::test_wf01_rejects_same_turn_model_propose_then_confirm -vv
1 failed — expected outcome `proposed`, received `confirmed`
```

After the guard:

```text
$ .venv/bin/python -m pytest -q tests/test_sales_agent_w4.py::test_wf01_rejects_same_turn_model_propose_then_confirm -vv
1 passed, 2 warnings

$ .venv/bin/python -m pytest -q -rs tests/test_agent_booking_phase4.py tests/test_sales_agent_w3.py tests/test_sales_agent_w4.py tests/test_reception_agent_phase5.py
38 passed, 2 warnings

$ .venv/bin/python -m pytest -q -rs
528 passed, 21 warnings in 351.67s
```

The final full suite is one serial real-PostgreSQL run. The pre-change
baseline at the same repository HEAD was `527 passed, 21 warnings`; current
collection is `528 tests` because this task adds one regression test. The
regression fake model calls `propose_appointment` and `confirm_appointment`
within one turn; the persisted result is one pending proposal, one inbound
message, zero appointments, and one audited confirmation-tool `INVALID_INPUT`.
The focused suite also preserves the valid later-message booking, cross-
conversation `NOT_FOUND`, expiry rejection, exactly-one appointment replay,
and reception cancellation behavior.

## Files changed

- `app/agent_tools/booking.py`
- `tests/test_agent_booking_phase4.py`
- `tests/test_sales_agent_w4.py`
- `tests/test_reception_agent_phase5.py`
- `CHANGELOG.md`
- this handoff

## Close-out

One bounded commit is required for this task, using the repository's standard
`fix:` prefix. The canonical Foreman living brief and Repo 0 CAVELOG are
updated separately in `odontoflow-planning`; no next activity is started.
