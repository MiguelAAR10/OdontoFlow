# AGENT-02 — Technical handoff

Date: 2026-09-24
Base: `02fb031c6949c1c5ea69278a9e9a33a4cbb17f72`
Status: **PASS — bounded failure recovery shipped; no next activity started**

## Result

Authenticated provider/runtime failures now make one bounded, best-effort call
through the existing typed `BackendGateway` to
`request_human_handoff`. The API preserves the existing `503 /
AGENT_EXECUTION_FAILED` response and sanitized diagnostic logging while the
canonical backend creates or reuses one tenant-bound open `ReceptionHandoff`.

The recovery trigger is explicitly limited to diagnostic categories
`provider_timeout`, `turn_timeout`, and `unknown`. Authentication,
permission, rate-limit/request-validation, gateway, invalid-response,
unavailable-runtime, unrelated transport, and recursion paths keep their
existing behavior and do not create a failure handoff.

`request_handoff` now permits the already-human-handoff conversation state only
for this command, so a repeated typed request reuses the existing open row;
all other reception tools retain the automation-active guard. The existing
UUIDv4 mutation-key contract and per-command receipts remain unchanged; no
whole-turn idempotency or new recovery domain was added.

## Privacy and authority boundary

- The Sales Agent reaches recovery only through the authenticated typed HTTP
  gateway; it has no database/session/model access to canonical state.
- Failure-created metadata is fixed to `reason_code: other` and the
  content-free summary: `Automatic assistance could not complete the
  conversation and human recovery is required.`
- Provider response bodies, exception text, stack traces, prompts, patient
  messages, credentials, and secrets are not copied into the handoff or public
  error envelope. Public details remain trace identifiers only.
- Handoff write failures are swallowed at the Sales Agent boundary, preserving
  the original failure response; the request claims durable recovery only when
  the backend command commits.

## RED/GREEN evidence

The implementation worker first verified genuine RED against the pre-fix code:
three focused failures showed excluded provider categories still attempted a
handoff and a repeated typed handoff returned `ENTITY_INACTIVE`. After the
bounded changes in `sales_agent/api.py`, `app/agent_tools/reception.py`, and
`tests/test_sales_agent_failure_handoff.py`, the focused file was GREEN with
10 tests passing.

Persisted post-repair verification (not rerun by the release writer):

```text
./.venv/bin/python -m pytest -q tests/test_sales_agent_failure_handoff.py
10 passed, 0 failed, 0 skipped, 2 warnings in 13.38s

./.venv/bin/python -m pytest -q
660 passed, 0 failed, 0 skipped, 21 warnings in 1280.60s (0:21:20)
```

The focused RED/GREEN and implementation evidence is persisted in
`task_47de38e92377`; the serial verification result is persisted in
`task_10023381cc73`. The independent D1 review is persisted in
`task_db21f6f7c931`. No tests were rerun during release staging.

## Review disposition and warnings

**D1: PASS.** The independent review accepted the owner contract: one open
tenant/conversation handoff is the business deduplication authority, while
distinct UUIDv4 command receipts and audit events are valid per-invocation
provenance and are not a second recovery state. The prior static warning about
duplicate receipt/audit effects was adjudicated against that contract and did
not identify a release defect.

Remaining warning: the focused exclusion parametrization directly exercises
provider authentication and rate-limit categories; other excluded branches
remain protected by the explicit recovery-category allowlist and existing
branch mappings. This is coverage expansion material, not a correctness or
release blocker.

## Files in the bounded change

- `sales_agent/api.py` — fixed recovery metadata, category allowlist, and
  authenticated typed handoff attempt.
- `app/agent_tools/reception.py` — allow repeated handoff requests to reuse the
  existing open row after human handoff.
- `tests/test_sales_agent_failure_handoff.py` — real-PostgreSQL contract tests
  for included/excluded failures, privacy, tenant isolation, replay/reuse,
  prior typed effects, and write-failure truthfulness.
- This handoff.

Unrelated pre-existing tracked and untracked dirt was preserved and is not
part of the release.

## Release

Exactly one bounded commit contains the three product paths and this handoff.
The release commit SHA, parent, remote SHA, staged-path inspection, checks, and
persisted test evidence are recorded in the release worker completion report.
