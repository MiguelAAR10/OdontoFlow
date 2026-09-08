# Handoff — reception continuity checkpoint

## Outcome and scope

An optional typed `reception_state` now travels with the outbound-message
creation request and is committed atomically with its message, delivery job and
audit event. `get_reception_context` exposes the latest retained checkpoint to
the next turn. No database migration is needed.

This feature is advisory continuity only. `pending_action` remains authoritative
for proposals; appointment, confirmation and handoff authorization logic is
unchanged. Deployment, infrastructure and real-channel operations are outside
this backend subtask.

## Contract and retention

- Request: optional `reception_state` on the existing outbound POST. Existing
  text-only requests remain valid. Unknown/nested extra fields are rejected.
- Bounded state: intent/phase, positive IDs, at most three typed slots,
  offset-aware timestamps, and a source inbound message ID. No names or tokens.
- Source inbound must be unexpired, unredacted and in this conversation/tenant.
- Storage: private `_reception_state` payload entry. A private digest preserves
  replay/conflict checks even after checkpoint content is redacted.
- Provider claim strips private payload entries. Logical text and the receipt
  never contain the internal checkpoint.
- Read: latest outbound carrying a checkpoint, with both owning outbound and
  source inbound content still visible. Invalid legacy metadata returns null.
- Null/omitted state preserves previous checkpoints; clearing a task requires an
  explicit completed checkpoint. The state does not carry proposal tokens.

## Verification

- Pre-flight backend working tree was clean.
- Baseline: **466 passed**, 21 warnings, **915.56 seconds**.
- TDD red: first new persistence case failed **422 instead of 201** before
  implementation; **1 failed**, 2 warnings, **13.82 seconds**.
- Focused continuity, messaging and reception suites: **49 passed**, 2 warnings,
  **97.20 seconds**. Includes 28 new checkpoint cases.
- The first positive focused run caught missing trace headers in the new test
  helper; fixing the helper preserved the backend trace-envelope guard. Review
  also tightened datetime strings to reject numeric-string epoch shortcuts.
- Final full-suite result: **494 passed**, 21 warnings, **823.73 seconds**
  (2026-09-05). Verified target `odontoflow_test`, port 5434, before starting.
- No concurrent pytest processes were run.
- Independent reviewer found numeric-string epoch acceptance; fixed using an
  ISO-aware input boundary and regression fixtures for scalar/slot timestamps.
  Final focused reviewer confirmation and commit remain with the orchestrator.

## Deployment review — 2026-09-08

An independent read-only review of the current diff, spec and tests found no
blocking issue for deploying this exact continuity snapshot. The deployment
must include these changes: n8n v2.1 sends `reception_state`. No new database
migration or dependency is required. The current lab database is at migration
0015; its data and existing integration credential hashes must be preserved.
Do not run the bootstrap script after restoring it because that rotates tokens.

Deployment revalidation: full suite on the isolated `odontoflow_test` database
finished with 493 passed / 1 failed in 1072.72 seconds. The single failure was
`test_rate_limit_is_shared_and_scoped_per_credential` (third request returned
200 instead of 429); its isolated rerun passed in 10.26 seconds without any code
change. The implementation uses wall-clock minute windows and the test does not
freeze time, so a minute-boundary crossing can produce that result. This run
must not be reported as a clean full-suite pass. The earlier 494-pass run remains
documented above. No application behavior was altered for deployment.

The sibling MediStock checkout already contained four user modifications:
`docker-compose.yml`, `src/clinica_backend/app/schemas/paciente_schema.py`,
`src/clinica_backend/app/services/inventario_service.py`, and
`src/sql/schema/01_create_schema.sql`. They were preserved and never edited.
