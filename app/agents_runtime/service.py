"""COB application services: start a run (tx1), sweep, settle; list runs.

Spec: ``docs/superpowers/specs/2026-10-01-erp-cob.md``. tx1 claims the
receipt first, then authorizes, checks the kill switch and the proposer, and
inserts the ``running`` row; the receipt settles there with ``{run_id}``, so a
same-key replay returns the run as it is now and never sweeps again.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents_runtime import cobranza, confirmaciones, inventario
from app.agents_runtime.errors import DISABLED, NOT_PROVISIONED, agent_disabled
from app.agents_runtime.models import AgentRun
from app.agents_runtime.schemas import AgentRunCounts, AgentRunOut, AgentRunPage
from app.audit.service import record_event
from app.config import _boolean_env
from app.errors import AppError, ErrorCode
from app.events.service import record_domain_event
from app.iam.context import ExecutionContext
from app.iam.models import Membership, Principal
from app.iam.permissions import (
    CHARGES_READ,
    MOVEMENTS_READ,
    PRODUCTS_READ,
    PROPOSALS_CREATE,
    PROPOSALS_DECIDE,
    PROPOSALS_READ,
)
from app.iam.service import (
    PERMISSION_DENIED_HTTP_STATUS,
    PERMISSION_DENIED_MESSAGE,
    IamErrorCode,
    has_permission,
    require_permission,
)
from app.idempotency.service import (
    IdempotencyClaim,
    claim_receipt,
    run_idempotent_command,
    settle_receipt,
)

OP_AGENT_RUN_START = "agent_run.start"
ENTITY_TYPE = "agent_run"
KILL_SWITCH_ENV = "AGENT_COBRANZA_ENABLED"
#: The org's collections proposer for human-triggered runs. A fixed server
#: constant: never taken from the body or from ``agent_key``.
COBRANZA_PROPOSER = "airy-cobranza"
#: INV: the org's inventory proposer for human-triggered runs (same rule).
INVENTARIO_PROPOSER = "airy-inventario"
INVENTARIO_KILL_SWITCH_ENV = "AGENT_INVENTARIO_ENABLED"
MACHINE_TYPES = ("agent", "integration")
#: The reads each proposing agent's sweep needs, on top of the trigger gate.
COBRANZA_READS = (CHARGES_READ,)
INVENTARIO_READS = (PRODUCTS_READ, MOVEMENTS_READ)


def cobranza_enabled() -> bool:
    return _boolean_env(KILL_SWITCH_ENV, True)


def inventario_enabled() -> bool:
    return _boolean_env(INVENTARIO_KILL_SWITCH_ENV, True)


def _deny() -> AppError:
    return AppError(
        IamErrorCode.PERMISSION_DENIED,
        PERMISSION_DENIED_MESSAGE,
        details={},
        http_status=PERMISSION_DENIED_HTTP_STATUS,
    )


def _authorize(session: Session, ctx: ExecutionContext, reads: tuple[str, ...]) -> None:
    """Machine callers need ``proposals.create``, humans ``proposals.decide``;
    both need the agent's ``reads``; ``system`` is refused."""
    if ctx.principal_type in MACHINE_TYPES:
        require_permission(session, ctx, PROPOSALS_CREATE)
    elif ctx.principal_type == "human":
        require_permission(session, ctx, PROPOSALS_DECIDE)
    else:
        raise _deny()
    for code in reads:
        require_permission(session, ctx, code)


def _proposer(
    session: Session, ctx: ExecutionContext, *, name: str, agent_key: str
) -> ExecutionContext:
    """Who proposes: the machine caller itself, or this org's fixed ``name`` agent."""
    if ctx.principal_type in MACHINE_TYPES:
        return ctx
    principal_id = session.scalar(
        select(Principal.id)
        .join(Membership, Membership.principal_id == Principal.id)
        .where(
            Principal.type == "agent",
            Principal.display_name == name,
            Membership.organization_id == ctx.organization_id,
            Membership.is_active.is_(True),
        )
        .order_by(Principal.id)
        .limit(1)
    )
    if principal_id is None or not has_permission(
        session, principal_id, ctx.organization_id, PROPOSALS_CREATE
    ):
        raise agent_disabled(agent_key, NOT_PROVISIONED)
    return ExecutionContext(
        organization_id=ctx.organization_id,
        principal_id=principal_id,
        principal_type="agent",
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
    )


def _audit(session: Session, ctx: ExecutionContext, run: AgentRun, action: str,
           before: str | None) -> None:
    record_event(
        session,
        ctx=ctx,
        entity_type=ENTITY_TYPE,
        entity_id=str(run.id),
        action=action,
        before_state={"status": before} if before else None,
        after_state={
            "status": run.status,
            "agent_key": run.agent_key,
            "counts": _counts(run).model_dump(),
            "error_category": run.error_category,
        },
    )


def start_run(
    session: Session,
    *,
    ctx: ExecutionContext,
    agent_key: str,
    idempotency: IdempotencyClaim | None = None,
) -> tuple[int, ExecutionContext]:
    """tx1: claim → authorize → kill switch → proposer → insert ``running``.

    Dispatches on ``agent_key`` before authorizing: each agent has its own gate
    (SELF: confirmaciones is human-only with ``appointments.read`` +
    ``deliveries.create`` and queues as the caller, so the caller is the actor).
    """
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        if agent_key == confirmaciones.AGENT_KEY:
            confirmaciones.authorize(session, ctx)
            if not confirmaciones.confirmaciones_enabled():
                raise agent_disabled(agent_key, DISABLED)
            proposer = ctx
        elif agent_key == cobranza.AGENT_KEY:
            _authorize(session, ctx, COBRANZA_READS)
            if not cobranza_enabled():
                raise agent_disabled(agent_key, DISABLED)
            proposer = _proposer(session, ctx, name=COBRANZA_PROPOSER, agent_key=agent_key)
        elif agent_key == inventario.AGENT_KEY:
            _authorize(session, ctx, INVENTARIO_READS)
            if not inventario_enabled():
                raise agent_disabled(agent_key, DISABLED)
            proposer = _proposer(session, ctx, name=INVENTARIO_PROPOSER, agent_key=agent_key)
        else:
            raise AppError(ErrorCode.INVALID_INPUT, "Unknown agent.")
        run = AgentRun(
            organization_id=ctx.organization_id,
            agent_key=agent_key,
            trigger="manual",
            status="running",
            triggered_by_principal_id=ctx.principal_id,
        )
        session.add(run)
        session.flush()
        _audit(session, ctx, run, "agent_run.started", None)
        settle_receipt(
            receipt,
            resource_type=ENTITY_TYPE,
            resource_id=str(run.id),
            outcome_json={"run_id": run.id},
        )
        run_id = run.id
    return run_id, proposer


def _lock(session: Session, organization_id: int, run_id: int) -> AgentRun:
    return session.scalar(
        select(AgentRun)
        .where(AgentRun.organization_id == organization_id, AgentRun.id == run_id)
        .with_for_update()
    )


def _complete(session: Session, ctx: ExecutionContext, run_id: int,
              counts: cobranza.Counts) -> None:
    with session.begin():
        run = _lock(session, ctx.organization_id, run_id)
        run.status = "completed"
        run.candidates_count = counts.candidates
        run.proposed_count = counts.proposed
        run.deduped_count = counts.deduped
        run.skipped_count = counts.skipped
        run.finished_at = func.now()
        session.flush()
        _audit(session, ctx, run, "agent_run.completed", "running")
        record_domain_event(
            session,
            ctx=ctx,
            event_type="agent_run.completed",
            aggregate_type=ENTITY_TYPE,
            aggregate_id=str(run.id),
            payload=counts.as_json(),
        )


def _fail(session: Session, ctx: ExecutionContext, run_id: int, category: str) -> None:
    session.rollback()
    with session.begin():
        run = _lock(session, ctx.organization_id, run_id)
        run.status = "failed"
        run.error_category = category
        run.finished_at = func.now()
        session.flush()
        _audit(session, ctx, run, "agent_run.failed", "running")


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: int
    replayed: bool


def run_agent(
    session: Session, *, ctx: ExecutionContext, agent_key: str, key: str | None
) -> RunResult:
    """Run the sweep synchronously; the agent only proposes, a human approves."""
    outcome = run_idempotent_command(
        session,
        operation=start_run,
        operation_name=OP_AGENT_RUN_START,
        key=key,
        ctx=ctx,
        params={"agent_key": agent_key},
        agent_key=agent_key,
    )
    if outcome.replayed:
        return RunResult(int(outcome.outcome["run_id"]), True)
    run_id, proposer = outcome.result
    try:
        if agent_key == confirmaciones.AGENT_KEY:
            counts = confirmaciones.sweep(session, caller=proposer)
        elif agent_key == inventario.AGENT_KEY:
            counts = inventario.sweep(session, run_id=run_id, proposer=proposer)
        elif agent_key == cobranza.AGENT_KEY:
            counts = cobranza.sweep(session, run_id=run_id, proposer=proposer)
        else:  # start_run already refused it; never fall through to another sweep
            raise AppError(ErrorCode.INVALID_INPUT, "Unknown agent.")
    except Exception as exc:
        category = exc.code.value if isinstance(exc, AppError) else "unexpected"
        _fail(session, ctx, run_id, category)
        raise
    _complete(session, ctx, run_id, counts)
    return RunResult(run_id, False)


# --- reads ----------------------------------------------------------------------


def _counts(run: AgentRun) -> AgentRunCounts:
    return AgentRunCounts(
        candidates=run.candidates_count or 0,
        proposed=run.proposed_count or 0,
        deduped=run.deduped_count or 0,
        skipped=run.skipped_count or 0,
    )


def run_out(run: AgentRun) -> AgentRunOut:
    return AgentRunOut(
        id=run.id,
        agent_key=run.agent_key,
        trigger=run.trigger,
        status=run.status,
        triggered_by_principal_id=run.triggered_by_principal_id,
        counts=_counts(run),
        error_category=run.error_category,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def get_run(session: Session, *, ctx: ExecutionContext, run_id: int) -> AgentRunOut:
    session.expire_all()
    with session.begin():
        run = session.scalar(
            select(AgentRun).where(
                AgentRun.organization_id == ctx.organization_id, AgentRun.id == run_id
            )
        )
        if run is None:
            raise AppError(ErrorCode.NOT_FOUND, "Agent run not found.")
        return run_out(run)


def list_runs(
    session: Session, *, ctx: ExecutionContext, agent_key: str | None, limit: int
) -> AgentRunPage:
    with session.begin():
        require_permission(session, ctx, PROPOSALS_READ)
        statement = select(AgentRun).where(AgentRun.organization_id == ctx.organization_id)
        if agent_key is not None:
            statement = statement.where(AgentRun.agent_key == agent_key)
        rows = session.scalars(
            statement.order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(limit)
        ).all()
        return AgentRunPage(items=[run_out(row) for row in rows])
