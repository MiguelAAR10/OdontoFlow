"""The backfill agent (BACKFILL): a cancelled slot becomes one ``waitlist_offer``.

Invoked per claimed ``agent_jobs`` row by the tick (``app/agent_jobs/service.py``).
No LLM: SQL matches the waitlist, code ranks by ``created_at`` and drafts a fixed
text, and the proposal goes through B2's ``create_proposal``; a human approves
before any patient is messaged. The run row and its audit are written under the
tick caller's context (like COB's manual runs); only ``create_proposal`` runs as
the proposer.

Run counts are per freed slot (``ck_agent_runs_counts``: proposed + deduped +
skipped = candidates): a slot that is proposed, deduped or skipped is one
candidate; a slot nobody reachable matches is zero. How many waitlist entries
matched lives in the proposal evidence (``matched``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.agents_runtime.cobranza import Counts
from app.agents_runtime.models import AgentRun
from app.agents_runtime.service import _audit, _complete, _fail
from app.catalog.models import Service
from app.errors import AppError, ErrorCode
from app.events.models import DomainEvent
from app.iam.context import ExecutionContext
from app.organization.models import Location
from app.proposals.executors import (
    APPOINTMENT_SUBJECT,
    MAX_OFFER_ENTRIES,
    OFFER_TTL,
    normalize_payload,
    reachable_conversation,
)
from app.proposals.models import AgentProposal
from app.proposals.service import create_proposal
from app.scheduling.models import Appointment
from app.scheduling.service import CANCELLED
from app.scheduling.waitlist import matching_open_entries, slot_is_free, slot_local_start

AGENT_KEY = "backfill"
KIND = "waitlist_offer"
SKIPPABLE = (ErrorCode.INVALID_INPUT, ErrorCode.NOT_FOUND)


def draft_message(*, service: str, local_start: datetime, location: str) -> str:
    minutes = int(OFFER_TTL.total_seconds() // 60)
    return (
        f"Hola, se liberó un cupo de {service} el {local_start:%d/%m} a las "
        f"{local_start:%H:%M} en {location}. Responde SÍ en los próximos {minutes} "
        "minutos para reservarlo."
    )


def start(session: Session, *, ctx: ExecutionContext, job) -> int:
    """Insert the ``running`` event-triggered run; the tick caller is ``triggered_by``."""
    with session.begin():
        run = AgentRun(
            organization_id=ctx.organization_id,
            agent_key=AGENT_KEY,
            trigger="event",
            status="running",
            triggered_by_principal_id=ctx.principal_id,
        )
        session.add(run)
        session.flush()
        _audit(session, ctx, run, "agent_run.started", None)
        return run.id


def process(session: Session, *, run_id: int, job, caller: ExecutionContext,
            proposer: ExecutionContext) -> Counts:
    """Propose and settle the run; any error fails the run and is re-raised."""
    try:
        counts = _propose(session, run_id=run_id, job=job, proposer=proposer)
    except Exception as exc:
        category = exc.code.value if isinstance(exc, AppError) else "unexpected"
        _fail(session, caller, run_id, category)
        raise
    _complete(session, caller, run_id, counts)
    return counts


def _propose(session: Session, *, run_id: int, job, proposer: ExecutionContext) -> Counts:
    org = proposer.organization_id
    with session.begin():
        payload = session.scalar(
            select(DomainEvent.payload).where(
                DomainEvent.organization_id == org, DomainEvent.id == job.source_event_id
            )
        )
        appointment_id = (payload or {}).get("appointment_id")
        appointment = (
            session.scalar(
                select(Appointment).where(
                    Appointment.organization_id == org, Appointment.id == int(appointment_id)
                )
            )
            if appointment_id is not None
            else None
        )
        now = datetime.now(timezone.utc)
        if (
            appointment is None
            or appointment.state != CANCELLED
            or appointment.start_utc <= now + OFFER_TTL
            or not slot_is_free(session, appointment)
        ):
            return Counts(candidates=1, skipped=1)
        if session.scalar(
            select(
                exists().where(
                    AgentProposal.organization_id == org,
                    AgentProposal.kind == KIND,
                    AgentProposal.subject_type == APPOINTMENT_SUBJECT,
                    AgentProposal.subject_id == str(appointment.id),
                )
            )
        ):
            return Counts(candidates=1, deduped=1)
        reachable = [
            entry.id
            for entry in matching_open_entries(session, org, appointment=appointment)
            if reachable_conversation(session, org, entry.patient_id) is not None
        ]
        if not reachable:
            return Counts(candidates=0)
        service = session.scalar(
            select(Service.name).where(
                Service.organization_id == org, Service.id == appointment.service_id
            )
        )
        location = session.scalar(
            select(Location.name).where(
                Location.organization_id == org, Location.id == appointment.location_id
            )
        )
        local_start = slot_local_start(session, appointment)
        start_utc = appointment.start_utc.astimezone(timezone.utc).isoformat()

    chosen = reachable[:MAX_OFFER_ENTRIES]
    raw = {
        "appointment_id": appointment.id,
        "entry_ids": chosen,
        "message_text": draft_message(service=service, local_start=local_start,
                                      location=location),
    }
    evidence = {
        "run_id": run_id,
        "job_key": job.job_key,
        "appointment_id": appointment.id,
        "start_utc": start_utc,
        "service": service,
        "location": location,
        "matched": len(reachable),
        "offered_to": chosen,
    }
    reason = (
        f"Cancelación: {service} {local_start:%d/%m %H:%M} en {location}; "
        f"{len(reachable)} pacientes en lista de espera, se ofrece a {len(chosen)}"
    )
    counts = Counts(candidates=1)
    args, normalized = normalize_payload(KIND, raw)
    try:
        _proposal, created = create_proposal(
            session,
            ctx=proposer,
            kind=KIND,
            args=args,
            payload=normalized,
            reason=reason,
            evidence=evidence,
            agent_key=AGENT_KEY,
        )
    except AppError as exc:
        if exc.code not in SKIPPABLE:
            raise
        counts.skipped += 1
        return counts
    if created:
        counts.proposed += 1
    else:
        counts.deduped += 1
    return counts
