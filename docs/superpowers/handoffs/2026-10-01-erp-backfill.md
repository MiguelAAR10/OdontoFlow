# Handoff — ERP-AGENTICO-01 · BACKFILL (leased agent jobs + cancellation backfill agent)

## Summary
- Migration `0026_agent_jobs` (head `0026`, down `0025`, additive): table `agent_jobs` (no payload column; the
  source `domain_events` row holds `appointment_id`), `UNIQUE(organization_id, job_key)`,
  `UNIQUE(organization_id, id)`, composite FK `(organization_id, source_event_id)` → `domain_events` RESTRICT,
  CHECKs on status / `agent_key IN ('backfill')` / `attempts >= 0` / "leased iff token + lease", partial index
  `ix_agent_jobs_org_due`. Widens `ck_agent_proposals_kind` (+`waitlist_offer`) and `ck_agent_runs_agent_key`
  (+`backfill`); ORM mirrors widened. Downgrade deletes the new rows, drops the table, restores the 0025 sets.
- `app/agent_jobs/service.py`: `enqueue_from_events` (one `INSERT … SELECT … ON CONFLICT DO NOTHING` over the
  caller's org `appointment.cancelled` events of the last 48 h whose slot starts within 48 h of the event →
  `backfill:event:<id>`); `claim_one`/`claim_in_tx` (org+agent-scoped dead-sweep of expired leases at
  `MAX_ATTEMPTS`, then `FOR UPDATE SKIP LOCKED` claim minting a new `lease_token`, lease 5 min); fenced `settle`
  (`done` / `failed` with `run_after = now() + 60 s × attempts` / `dead` at 3; 0 rows → `LeaseLost`);
  kill switch `AGENT_BACKFILL_ENABLED` read before every claim; `run_due` tick (authorize → enqueue → proposer
  resolved once before any claim → claim/handle/settle loop).
- `POST /agent-runs/jobs/run-due {limit 1..20, default 10}` on the mounted `agent_runs_router` (no
  `app/__init__.py` change), 200 `JobsRunOut{enqueued, claimed, done, failed, dead, lost, disabled_agents, jobs}`.
  Gate as COB (agents `proposals.create`, humans `proposals.decide`, `system` refused) + `appointments.read` +
  `waitlist.read`. Human tick proposes as `airy-backfill` (409 `AGENT_DISABLED reason=not_provisioned` leaves
  jobs `queued`, attempts 0). Profile `backfill-agent` in `issue_credential.py`; CLI `odontoflow jobs run-due
  [--limit N]`. `RunAgentKey` (+`backfill`, also the `GET /agent-runs?agent_key=` filter) and `InboxKind`
  (+`waitlist_offer`) widened; `AgentKey` and `ProposalKindName` stay closed (422).
- Handler `app/agents_runtime/backfill.py`: run `backfill/event` triggered by the tick caller; skip (event or
  appointment missing, not cancelled, starts within `OFFER_TTL`, slot taken practitioner-globally), dedupe (any
  `waitlist_offer` on `appointment:<id>`), match with `matching_open_entries` (one SQL query, ≤ 50) filtered by
  `reachable_conversation`, offer the 3 oldest; counts are per freed slot, `matched` lives in the evidence.
- Kind `waitlist_offer` (`deliveries.create`, TTL 30 min, subject `appointment:<id>`, version
  `"{free|taken}|{open entry ids}"`). Execute as the approver: `offer_waitlist_entries` (`waitlist.manage`,
  appointment still cancelled + slot free, entries locked org-scoped and re-matched, `open → offered`, audit
  `waitlist_entry.offered` + domain event `waitlist.offered`), then one outbox message per offered entry with a
  deterministic UUIDv4-shaped key (`offer_message_key`), so replays never duplicate.
  Spec: `docs/superpowers/specs/2026-10-01-erp-backfill.md`.

## Review of the inherited work (this session)
The implementation was inherited uncommitted and reviewed against the spec's Contract, Invariants and the 11
acceptance tests. Code matched the contract; the inherited focused tests were green (28 passed). Two acceptance
gaps were closed test-first in `tests/test_backfill.py`:
- Acceptance 9, last clause: "slot rebooked after tx1 (inside execute) → proposal `failed`, 0 offered,
  0 messages" was only exercised by calling `offer_waitlist_entries` directly. New
  `test_a_slot_rebooked_after_approval_fails_the_offer_and_sends_nothing` drives it through
  `POST /agent/proposals/{id}/approve` (rebook committed from a second session after the version check).
  Mutation check: removing the execute-time slot re-check turns it red (`executed` instead of `failed`).
- Contract "`AgentKey` (POST /agent-runs) stays closed → 422": new
  `test_backfill_runs_are_never_started_by_post_agent_runs`.
No production code changed in this session.

## Evidence
- Full serial suite with `.env.local`: **937 passed, 0 failed**, 53 warnings (1813.71 s).
- Focused: `tests/test_agent_jobs.py tests/test_backfill.py tests/test_migrations.py tests/test_agent_proposals.py`
  → 72 passed (132.42 s).
- `create_app().openapi() == docs/api/openapi.json` → True (YAML equal too); `alembic heads` → `0026 (head)`.
- `tests/test_agent_jobs.py` (15): deterministic SKIP LOCKED exclusion over 1 and 2 jobs (thread + `Event`),
  lease expiry reclaim + fenced stale settle (`LeaseLost`), dead at max attempts with tenant-scoped sweep,
  failure backoff → dead, handler failure fails job and run, kill switch, enqueue dedupe/horizon/event type,
  tenant isolation, gate (403 reads / system, 409 not_provisioned with attempts 0), body 422, CHECKs, CLI.
- `tests/test_backfill.py` (15; 13 inherited + 2 new): demo (4 matching + 4 decoys → offer to the 3 oldest, evidence `matched=4`,
  proposer `airy-backfill`, run `backfill/event/completed` triggered by Lucía, listed by
  `GET /agent-runs?agent_key=backfill`), far/unmatched and rebooked skips, repeated/requeued ticks and decline
  never duplicate, Lucía approves → 3 `offered` + 3 outbound + audit + events, replay keeps 3, drift
  supersedes, permission split, execute-time slot recheck (function and HTTP), foreign entry ids ignored,
  HTTP proposal of the kind 422, `backfill` POST /agent-runs 422, L4 pin, profile pin.
- `tests/test_migrations.py`: 0026 round-trip and CHECKs.

## Deviations (all declared in the spec)
- `tests/test_agent_proposals.py`: pinned `set(KINDS)` extended with `waitlist_offer`.
- `app/agents_runtime/router.py` / `schemas.py`: route on the mounted `/agent-runs` router; list filter typed
  `RunAgentKey`.
- `scripts/seed_demo.py` NOT changed (rejected deviation); live demo seed is the follow-up card BACKFILL-SEED.

## Deviations found at fan-in (outside the spec's list)
- `tests/test_tenant_integrity.py`: the pinned tenant-owned table set gains `agent_jobs` (its `organization_id`
  is NOT NULL with `fk_agent_jobs_organization` RESTRICT, as the spec requires).
- `tests/test_security_boundary.py::test_rate_limit_is_shared_and_scoped_per_credential`: pre-existing,
  time-dependent flake, not caused by BACKFILL (no BACKFILL file touches rate limiting). Root cause:
  `claim_rate_limit` is a fixed wall-clock-minute window (`app/iam/credentials.py:252`); requests straddling
  a minute boundary land in two windows, so the third request returned 200. Reproduced deterministically by
  pinning the claim clock across a boundary (3 × 200). The test now pins every claim to one mid-minute
  instant; counters stay in PostgreSQL and the per-credential scope, 429 envelope and `Retry-After` are still
  asserted. Production limiter unchanged.

## Merge danger / risks
- Subject resolver raises `NOT_FOUND` when the appointment row is gone (spec phrases the resolver failures as
  `INVALID_INPUT`); the handler treats both as `skipped`, so behavior is the same. It also re-checks
  reachability (stricter than "open + matching").
- `normalize_payload` runs outside `_propose`'s `create_proposal` try/except: an `INVALID_INPUT` there (e.g. a
  message over 1000 chars from very long service/sede names) fails the run and the job retries to `dead`
  instead of counting `skipped`.
- `has_due` also counts expired leases at `MAX_ATTEMPTS` (only dead-swept, never claimed): a human tick without
  `airy-backfill` gets 409 even when only a sweep is due.
- With one agent key the "disabled agent's rows are never swept" check is structural (no keys → no sweep, no
  claim); it becomes meaningful when a second job agent is added.
- Accepted residuals (spec): a stale worker's proposal creation is deduped, not fenced; an outbox failure after
  `offered` leaves that entry offered without a message (proposal `failed`); a booking between the offer commit
  and the outbox enqueue still sends the offer. No `offered → open/expired` lapse yet (SIM card).
- `openapi.json/yaml` regenerated; merges touching `RunAgentKey`/`InboxKind` enums will conflict.
