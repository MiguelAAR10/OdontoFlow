# W2 Handoff — Conversation listing and close transition

Date: 2026-09-05
Workstream: W2 — Conversation Listing + Close Transition
Owner: GPT-5.6 Luna Max via Orca
Status: implementation and verification complete; this document ships in the
single W2 commit

## Objective

Enable an authenticated orchestration client to query eligible conversations
for follow-up and close a completed conversation, without adding a table or an
Alembic revision. W3 Sales Agent runtime, W4 n8n workflow implementation,
follow-up scheduling, promotions/pricing, frontend, infrastructure, and
appointment rescheduling/cancellation remain out of scope.

## API contract

### `GET /internal/conversations`

The route is behind the existing router-level bearer gate and calls the
existing `conversations.read` permission check. It accepts:

- `status`: optional one of `open`, `awaiting_confirmation`,
  `human_handoff`, `closed`.
- `last_message_before`: optional timezone-aware ISO-8601 instant. The
  predicate is exclusive (`last_message_at < last_message_before`).
- `limit`: optional integer from 1 through 100, default 50.

Results are tenant-scoped by the authenticated execution context and ordered
deterministically by `last_message_at ASC, id ASC`. The typed response contains
only:

```json
{
  "conversation_id": 123,
  "contact_identity_id": 456,
  "status": "open",
  "last_message_at": "2026-08-20T14:00:00Z"
}
```

The query uses the existing `ix_conversations_org_last_message` key shape
(`organization_id`, `last_message_at`); no new index is introduced.

### `POST /internal/conversations/{conversation_id}/close`

The route requires the existing canonical UUIDv4 `Idempotency-Key` header and
the authenticated principal's `conversations.manage` permission. It returns:

```json
{
  "conversation_id": 123,
  "status": "closed",
  "replayed": false
}
```

The command claims the existing PostgreSQL `command_receipts` row before the
permission/read path, locks the tenant-scoped conversation row, rejects a
missing or cross-tenant conversation as `NOT_FOUND`, and rejects an already
closed conversation as deterministic `ENTITY_INACTIVE`. The status transition,
`conversation.closed` audit event, and idempotency receipt commit atomically.
Reusing the same key replays the stored outcome; a new key after closure gets
the stable conflict and does not create another audit row.

Closing changes only `Conversation.status` and `updated_at`. Because the
existing `uq_conversations_active_contact` index is partial on
`status <> 'closed'`, an inbound message for the same tenant/channel/contact creates a
new open conversation after closure. This preserves exactly one open
conversation per contact while releasing the contact for deterministic reopen.

## Evidence

Pre-flight re-verification:

```text
git status --short --branch  main...origin/main with only pre-existing setup/documentation changes
git rev-parse HEAD          3291787e9184c673fecceec403fc33fa347d5d8c
git rev-parse origin/main   3291787e9184c673fecceec403fc33fa347d5d8c
.venv/bin/python -m pytest -q   471 passed, 0 failed, 21 warnings
```

Risk-weighted tests were written before production code and watched fail with
the expected 404 missing-route failures:

```text
.venv/bin/python -m pytest -q tests/test_messaging_w2.py
7 failed (routes absent), 2 warnings
```

After implementation:

```text
.venv/bin/python -m pytest -q tests/test_messaging_phase2.py tests/test_messaging_w2.py
22 passed, 2 warnings

.venv/bin/python -m pytest -q
478 passed, 0 failed, 21 warnings
```

The W2 tests prove authentication, tenant isolation, typed response fields,
status filtering, bounded limit, exclusive cutoff, audit and idempotent close,
deterministic repeat/error behavior, cross-tenant `NOT_FOUND`, and release/reopen
of the one-open-conversation invariant. The existing phase-2 messaging suite
also remains green.

Additional verification:

```text
git diff --check                                      PASS
.venv/bin/python -m compileall -q app scripts tests   PASS
.venv/bin/alembic heads                               0015 (head)
git diff --name-only -- alembic/versions              empty
.venv/bin/python scripts/generate_openapi.py          PASS
```

The generated OpenAPI documents are structurally equal. They retain all 38
existing paths and add exactly `/internal/conversations` and
`/internal/conversations/{conversation_id}/close`, plus the two typed schemas
`ConversationRead` and `ConversationCloseReceipt`; no existing path was
removed or changed. No Alembic revision or migration file was modified.

## Files

Implementation and tests:

- `app/messaging/router.py`
- `app/messaging/schemas.py`
- `app/messaging/service.py`
- `tests/test_messaging_w2.py`
- `CHANGELOG.md`
- `docs/api/openapi.json`
- `docs/api/openapi.yaml`
- this handoff

Protected `app/errors.py`, `app/db.py`,
`app/scheduling/availability.py`, existing ERP schemas, and
`alembic/versions/**` were not modified. Pre-existing setup/documentation
changes in `AGENTS.md`, `.agents/`, `.claude/`, `.playwright-mcp/`,
`docs/integration/TEAM_SYNC_AND_BRAND_TRANSFER.md`, and `skills-lock.json`
were preserved.

## Commit and push

One normal W2 commit is created with the repository convention:

```text
feat: add conversation listing and close transition
```

The exact commit SHA is the `HEAD` reported by `git rev-parse HEAD` after this
commit; it contains this handoff. The branch is pushed normally with
`git push origin main` (no force-push, reset, rebase, squash, or amend). The
worker completion report records the resulting SHA and push output.

## Blockers

None. W3 was not started.
