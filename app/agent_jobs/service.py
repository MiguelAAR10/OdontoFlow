"""BACKFILL job runner: enqueue from domain events, claim with lease + fencing, settle.

Spec: ``docs/superpowers/specs/2026-10-01-erp-backfill.md``. No daemon: a tick
(``run_due``, behind ``POST /agent-runs/jobs/run-due``) is invoked explicitly by
the CLI or n8n. PostgreSQL is the authority:

* one job per fact — ``UNIQUE(organization_id, job_key)`` + ``ON CONFLICT DO NOTHING``;
* mutual exclusion — the claim picks its row with ``FOR UPDATE SKIP LOCKED``;
* fencing — every claim mints a new ``lease_token`` and ``settle`` only matches
  that token, so a worker whose lease expired and was reclaimed cannot settle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents_runtime import backfill
from app.agents_runtime.service import _authorize, _proposer
from app.config import _boolean_env
from app.errors import AppError
from app.iam.context import ExecutionContext
from app.iam.permissions import APPOINTMENTS_READ, WAITLIST_READ

#: Policy defaults (coordinator's choice, stated in the spec).
LEASE = timedelta(minutes=5)
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(seconds=60)  # × attempts
BACKFILL_HORIZON = timedelta(hours=48)
RUN_DUE_DEFAULT_LIMIT = 10
RUN_DUE_MAX_LIMIT = 20
BACKFILL_KEY = backfill.AGENT_KEY
AGENT_KEYS = (BACKFILL_KEY,)
KILL_SWITCH_ENVS = {BACKFILL_KEY: "AGENT_BACKFILL_ENABLED"}
#: The org's backfill proposer for human-triggered ticks (fixed server constant).
BACKFILL_PROPOSER = "airy-backfill"
BACKFILL_READS = (APPOINTMENTS_READ, WAITLIST_READ)
CANCELLED_EVENT = "appointment.cancelled"


class LeaseLost(Exception):
    """The job is no longer leased with this token: the outcome is discarded."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    organization_id: int
    job_key: str
    agent_key: str
    source_event_id: int
    lease_token: UUID
    attempts: int


def enabled_keys() -> list[str]:
    """Per-agent kill switch, read before every claim (default on)."""
    return [key for key in AGENT_KEYS if _boolean_env(KILL_SWITCH_ENVS[key], True)]


# --- enqueue --------------------------------------------------------------------

_ENQUEUE = text(
    """
    INSERT INTO agent_jobs (organization_id, agent_key, job_key, source_event_id, run_after,
                            status)
    SELECT e.organization_id, :agent_key, 'backfill:event:' || e.id, e.id, now(), 'queued'
      FROM domain_events e
     WHERE e.organization_id = :org
       AND e.event_type = :event_type
       AND e.occurred_at >= now() - CAST(:horizon AS interval)
       AND e.payload ? 'start_utc'
       AND (e.payload->>'start_utc')::timestamptz > e.occurred_at
       AND (e.payload->>'start_utc')::timestamptz <= e.occurred_at + CAST(:horizon AS interval)
    ON CONFLICT (organization_id, job_key) DO NOTHING
    """
)


def _enqueue(session: Session, organization_id: int) -> int:
    result = session.execute(
        _ENQUEUE,
        {"org": organization_id, "agent_key": BACKFILL_KEY, "event_type": CANCELLED_EVENT,
         "horizon": BACKFILL_HORIZON},
    )
    return result.rowcount or 0


def enqueue_from_events(session: Session, ctx: ExecutionContext) -> int:
    """One job per near cancellation of the caller's org; returns rows inserted."""
    with session.begin():
        return _enqueue(session, ctx.organization_id)


# --- claim ----------------------------------------------------------------------

_DUE = (
    "organization_id = :org AND agent_key = ANY(CAST(:keys AS text[])) AND "
    "((status IN ('queued', 'failed') AND run_after <= now()) OR "
    "(status = 'leased' AND leased_until <= now()))"
)
_DEAD_SWEEP = text(
    """
    UPDATE agent_jobs
       SET status = 'dead', lease_token = NULL, leased_until = NULL,
           last_error = COALESCE(last_error, 'lease_expired'), updated_at = now()
     WHERE organization_id = :org AND agent_key = ANY(CAST(:keys AS text[]))
       AND status = 'leased' AND leased_until <= now() AND attempts >= :max_attempts
    """
)
_CLAIM = text(
    f"""
    UPDATE agent_jobs
       SET status = 'leased', lease_token = gen_random_uuid(),
           leased_until = now() + CAST(:lease AS interval), attempts = attempts + 1,
           updated_at = now()
     WHERE id = (SELECT id FROM agent_jobs WHERE {_DUE}
                  ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED)
    RETURNING id, organization_id, job_key, agent_key, source_event_id, lease_token, attempts
    """
)
_HAS_DUE = text(f"SELECT EXISTS (SELECT 1 FROM agent_jobs WHERE {_DUE})")


def claim_in_tx(session: Session, organization_id: int, keys) -> ClaimedJob | None:
    """Dead-sweep then claim one due job, inside the caller's transaction."""
    keys = list(keys)
    if not keys:
        return None
    params = {"org": organization_id, "keys": keys}
    session.execute(_DEAD_SWEEP, {**params, "max_attempts": MAX_ATTEMPTS})
    row = session.execute(_CLAIM, {**params, "lease": LEASE}).one_or_none()
    return ClaimedJob(*row) if row is not None else None


def claim_one(session: Session, organization_id: int, keys) -> ClaimedJob | None:
    with session.begin():
        return claim_in_tx(session, organization_id, keys)


def has_due(session: Session, organization_id: int, keys) -> bool:
    keys = list(keys)
    if not keys:
        return False
    return bool(session.scalar(_HAS_DUE, {"org": organization_id, "keys": keys}))


# --- settle ---------------------------------------------------------------------

_SETTLE = text(
    """
    UPDATE agent_jobs
       SET status = CASE WHEN :ok THEN 'done'
                         WHEN attempts >= :max_attempts THEN 'dead'
                         ELSE 'failed' END,
           run_after = CASE WHEN NOT :ok AND attempts < :max_attempts
                            THEN now() + CAST(:delay AS interval) * attempts
                            ELSE run_after END,
           lease_token = NULL, leased_until = NULL,
           last_error = :error, updated_at = now()
     WHERE organization_id = :org AND id = :id AND status = 'leased' AND lease_token = :token
    RETURNING status
    """
)


def settle(session: Session, organization_id: int, job_id: int, token: UUID, *, ok: bool,
           error: str | None = None) -> str:
    """Fenced settle: ``done`` / ``failed`` (backoff) / ``dead``; else ``LeaseLost``."""
    with session.begin():
        status = session.scalar(
            _SETTLE,
            {"org": organization_id, "id": job_id, "token": token, "ok": ok,
             "max_attempts": MAX_ATTEMPTS, "delay": RETRY_DELAY,
             "error": None if ok else (error or "unexpected")},
        )
    if status is None:
        raise LeaseLost(job_id)
    return status


# --- the tick -------------------------------------------------------------------


@dataclass(slots=True)
class JobResult:
    id: int
    job_key: str
    agent_key: str
    status: str
    attempts: int
    run_id: int | None
    last_error: str | None


@dataclass(slots=True)
class TickResult:
    enqueued: int = 0
    claimed: int = 0
    done: int = 0
    failed: int = 0
    dead: int = 0
    lost: int = 0
    disabled_agents: list[str] = field(default_factory=list)
    jobs: list[JobResult] = field(default_factory=list)


def _disabled(keys) -> list[str]:
    return [key for key in AGENT_KEYS if key not in keys]


def run_due(session: Session, *, ctx: ExecutionContext,
            limit: int = RUN_DUE_DEFAULT_LIMIT) -> TickResult:
    """Authorize → enqueue → (proposer once, before any claim) → claim/handle/settle."""
    org = ctx.organization_id
    result = TickResult()
    with session.begin():
        _authorize(session, ctx, BACKFILL_READS)
        result.enqueued = _enqueue(session, org)

    keys = enabled_keys()
    result.disabled_agents = _disabled(keys)
    if not keys:
        return result
    with session.begin():
        if not has_due(session, org, keys):
            return result
        # Resolved before the first claim: a 409 here leaves every job queued
        # with its attempts untouched.
        proposer = _proposer(session, ctx, name=BACKFILL_PROPOSER, agent_key=BACKFILL_KEY)

    for _ in range(limit):
        keys = enabled_keys()
        if not keys:
            result.disabled_agents = _disabled(keys)
            break
        job = claim_one(session, org, keys)
        if job is None:
            break
        result.claimed += 1
        run_id, error = None, None
        try:
            run_id = backfill.start(session, ctx=ctx, job=job)
            backfill.process(session, run_id=run_id, job=job, caller=ctx, proposer=proposer)
        except Exception as exc:  # noqa: BLE001 — every outcome settles the job
            session.rollback()
            error = exc.code.value if isinstance(exc, AppError) else "unexpected"
        try:
            status = settle(session, org, job.id, job.lease_token, ok=error is None, error=error)
        except LeaseLost:
            status = "lost"
        setattr(result, status, getattr(result, status) + 1)
        result.jobs.append(JobResult(id=job.id, job_key=job.job_key, agent_key=job.agent_key,
                                     status=status, attempts=job.attempts, run_id=run_id,
                                     last_error=error))
    return result
