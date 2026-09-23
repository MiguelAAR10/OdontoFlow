# AGENT-03 — Human-controlled appointment mutations

**Status:** IMPLEMENTED_LOCAL / COMMIT_BLOCKED — owner decision pending  
**Date:** 2026-09-22  
**Backend base HEAD:** `59be27678d7cc46041befe93206242f34ed145de`  
**Current backend HEAD:** `59be27678d7cc46041befe93206242f34ed145de` (changes remain local)

## Business result

AIRI agent principals can propose cancellation or rescheduling but cannot
execute confirmation. The existing human confirmation flows continue to work.
Agent refusal occurs before command transactions and receipt claims, and before
tool adapters can return a stored successful receipt.

## Changed files

- `app/agent_tools/guards.py` — owns the shared `require_human_confirmation`
  guard.
- `app/agent_tools/booking.py` — imports the shared guard, preserving the
  existing booking confirmation refusal and its existing call sites.
- `app/agent_tools/reception.py` — invokes the guard at the start of both
  cancellation/rescheduling command functions and both tool adapters.
- `tests/test_reception_agent_phase5.py` — covers agent refusal for pending and
  already-confirmed proposals through direct and tool command paths, plus
  stored cancellation and rescheduling receipt replay. Tests grant the agent
  the relevant IAM permissions so the confirmation guard is the deciding rule.
- `CHANGELOG.md` — records the shipped behavior.

No migration, API schema, OpenAPI, scheduling algorithm, provider, or frontend
file changed.

## Preserved contracts

The existing IAM permission checks, server-derived `ExecutionContext`,
tenant-scoped lookups, proposal binding and expiry, appointment transaction,
audit, and idempotency implementations remain in place. The shared guard
checks the existing server-resolved `principal_type`; it contains no duplicate
permission or domain logic. The existing agent booking confirmation refusal
uses that same function.

The human cancellation and rescheduling success tests passed. Proposal creation
and the existing agent booking refusal also passed in the focused reception
regression file.

## Test evidence

The test target was the local Docker PostgreSQL 15 test database
`odontoflow_test`, bound at `127.0.0.1:5434` and explicitly supplied to both
`DATABASE_URL` and `TEST_DATABASE_URL` for every pytest invocation. No cloud,
production, or remote database was contacted.

- **RED:** before product changes, the six initial focused cases failed because
  all four direct/tool command paths and both stored receipt replay paths did
  not raise the expected `INVALID_INPUT` refusal.
- **GREEN:** after implementation and expansion to pending and
  already-confirmed proposals, focused AGENT-03 regressions passed: **10 passed,
  9 deselected**.
- Reception file: **15 passed** before the final test matrix expansion; the
  final full suite includes the expanded cases.
- Related IAM, tenant, idempotency, booking, agent-tool, security-boundary and
  reception regression files: **166 passed** before the final four-case test
  matrix expansion.
- Final full serial PostgreSQL backend suite: **650 passed, 21 warnings** in
  8m59s. No tests were deleted, weakened, skipped, or xfailed.

The warning output was limited to existing Starlette/Alembic deprecations.

## Security review

An independent read-only reviewer returned PASS with no material security
finding. The review checked the authoritative commands, receipt replay order,
shared IAM/domain guard, human path, and import-cycle risk. It did not run tests
or touch the database. It noted that it did not exercise a fresh agent bearer
request through HTTP; the regression tests construct an `ExecutionContext`
from the persisted principal type and exercise the direct command and tool
adapter boundaries. The production tool route continues to derive context via
the existing HTTP identity resolver.

Coordinator diff review confirmed that the guard is invoked before direct
command `session.begin()`/receipt claims and before each tool adapter calls
`run_idempotent_command()`. Thus an agent cannot use the already-confirmed
proposal shortcut or an existing stored receipt to obtain a successful result.
The scoped credential-pattern scan found no credential-like values across the
seven intended implementation, changelog, and handoff files. `git diff --check`
reported no whitespace errors.

## Execution and publication

Execution was direct in this session and was **not supervised by Orca**. No
Orca settings, Run, Task, Dispatch, or terminal state was changed. The prior
Orca Run/Task/failed Dispatch remain historical records of the launcher failure
and were not reused for product execution.

No commit was created and nothing was pushed. Backend `AGENTS.md` requires a
fan-in gate including a clean MediStock worktree before the orchestrator
commits. The read-only MediStock status check found an unrelated untracked
`.audit/` directory. It was preserved. No commit SHA or remote SHA exists yet.
The backend working tree also contained pre-existing unrelated dirty and
untracked files; they were not staged or changed by this task.

## Security follow-up and next action

During the initial pre-implementation search, output from `.env.local`
included a remote database URL credential. No connection was made with that
URL. Treat that credential as exposed: the Planning coordinator should
coordinate its rotation/revocation and verify the replacement is kept out of
logs. Do not include the credential value in follow-up records.

The Planning coordinator has recorded the direct, unsupervised completion and
the exact commit gate in the existing activity control plane and CAVELOG. Do
not start another Activity Card as part of AGENT-03.

## Delivery recovery (2026-09-22)

The current backend branch remains main at base/current HEAD
59be27678d7cc46041befe93206242f34ed145de; local origin/main points to the
same SHA and the branch is neither ahead nor behind. The AGENT-03 change set
is limited to the five authorized implementation/test/changelog files listed
above and this handoff. Git status also contains unrelated pre-existing dirty
and untracked work, including .gitignore and AGENTS.md; it remains untouched.
No AGENT-03 files are staged, no commit exists, and no push occurred.

The reported test and review evidence was compared to the final diff by path
and call path. The reports do not include raw pytest output or content hashes,
so they provide no cryptographic tree attestation. The independent reviewer
returned PASS on the confirmation guard/replay behavior, tenant-scoped command
paths, human behavior, shared IAM helper, and import-cycle risk. Its report
noted that the changelog and handoff had not yet been updated at that point;
those documentation-only files were added afterward. It did not run tests or
touch the database, and no product/test path changed after review. No suite was
rerun during delivery recovery.

Backend AGENTS.md requires a clean MediStock tree and coordinator fan-in
before commit. This is a coordinator-enforced repository rule: no
core.hooksPath is configured, and the default hooks directory contains only
sample hooks. Read-only Git metadata confirms MediStock HEAD
ef2fffb7a348aa621f7a5b387e09a1553351000f and one untracked .audit/
directory, absent from the index and HEAD. The existing planning handoff had
recorded it before this recovery. Git cannot identify its original creator or
creation time. AGENT-03 has no write path into MediStock, and no evidence
indicates this activity created the directory. MediStock remains untouched.

The one unresolved decision is whether to approve a documented one-time
exception to the MediStock-clean condition. Approval would allow one scoped
local AGENT-03 backend commit only; it would not stage, remove, move, or inspect
MediStock .audit/, and would not authorize publication. Without approval,
the implementation remains local and uncommitted.

Separate follow-up: the remote database URL credential referenced by backend
.env.local appeared in earlier tool output. Its value is intentionally
omitted; it was not read or used during this recovery. Action requested of the
authorized credential owner: rotate or revoke. Completion is unconfirmed. No
production system was accessed or changed.
