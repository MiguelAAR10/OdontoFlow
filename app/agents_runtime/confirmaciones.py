"""SELF D-1 reminders (``agent_key='confirmaciones'``): SQL picks, a fixed template sends.

Spec: ``docs/superpowers/specs/2026-10-01-erp-self.md``. One org-scoped query
selects tomorrow's confirmed appointments (clinic timezone) with their latest
reachable conversation; each one gets at most one fixed-template WhatsApp
message per ``(appointment, start)`` through ``enqueue_outbound_message``,
queued **as the human caller** (L1 by coordinator decision Q12; only a human
may start the run, so a person triggers every patient-visible message).

Each enqueue commits its own transaction: a run that fails mid-sweep keeps the
reminders already queued and its counts are not authoritative; the rerun
dedupes them on the derived key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents_runtime.cobranza import Counts
from app.config import _boolean_env
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import APPOINTMENTS_READ, DELIVERIES_CREATE
from app.iam.service import (
    PERMISSION_DENIED_HTTP_STATUS,
    PERMISSION_DENIED_MESSAGE,
    IamErrorCode,
    require_permission,
)
from app.messaging.service import enqueue_outbound_message

AGENT_KEY = "confirmaciones"
KILL_SWITCH_ENV = "AGENT_CONFIRMACIONES_ENABLED"
MAX_CANDIDATES = 200
#: The conversation closed meanwhile (or vanished): nothing to send to.
SKIPPABLE = (ErrorCode.NOT_FOUND, ErrorCode.ENTITY_INACTIVE)

_SELECT = text(
    """
    SELECT a.id, a.start_utc, l.timezone, l.name, s.name,
           COALESCE(p.full_name, ld.full_name), c.id
    FROM appointments a
    JOIN locations l ON l.organization_id = a.organization_id AND l.id = a.location_id
    JOIN services s ON s.organization_id = a.organization_id AND s.id = a.service_id
    JOIN leads ld ON ld.organization_id = a.organization_id AND ld.id = a.lead_id
    LEFT JOIN patients p ON p.organization_id = a.organization_id AND p.id = a.patient_id
    LEFT JOIN LATERAL (
        SELECT cv.id
        FROM conversations cv
        JOIN contact_identities ci
          ON ci.organization_id = cv.organization_id AND ci.id = cv.contact_identity_id
        WHERE cv.organization_id = a.organization_id
          AND cv.status <> 'closed'
          AND ci.consent_status <> 'opted_out'
          AND (ci.lead_id = a.lead_id
               OR ci.patient_id = a.patient_id
               OR ci.normalized_phone_e164 = ld.contact_phone)
        ORDER BY cv.last_message_at DESC, cv.id DESC
        LIMIT 1
    ) c ON true
    WHERE a.organization_id = :org
      AND a.state = 'confirmed'
      AND (a.start_utc AT TIME ZONE l.timezone)::date
          = (now() AT TIME ZONE l.timezone)::date + 1
    ORDER BY a.start_utc, a.id
    LIMIT :limit
    """
)


@dataclass(frozen=True, slots=True)
class Candidate:
    appointment_id: int
    start_utc: datetime
    timezone: str
    location_name: str
    service_name: str
    full_name: str
    conversation_id: int | None


def confirmaciones_enabled() -> bool:
    return _boolean_env(KILL_SWITCH_ENV, True)


def authorize(session: Session, ctx: ExecutionContext) -> None:
    """Human only, with ``appointments.read`` + ``deliveries.create``."""
    if ctx.principal_type != "human":
        raise AppError(
            IamErrorCode.PERMISSION_DENIED,
            PERMISSION_DENIED_MESSAGE,
            details={},
            http_status=PERMISSION_DENIED_HTTP_STATUS,
        )
    require_permission(session, ctx, APPOINTMENTS_READ)
    require_permission(session, ctx, DELIVERIES_CREATE)


def reminder_key(organization_id: int, appointment_id: int, start_utc: datetime) -> str:
    """Deterministic UUIDv4 per ``(org, appointment, start)``: a reschedule gets its own."""
    seed = f"reminder_d1:{organization_id}:{appointment_id}:{start_utc.isoformat()}"
    return str(UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4))


def draft_reminder(item: Candidate) -> str:
    """The fixed Spanish template; pure (no Session, no LLM)."""
    first_name = (item.full_name.split() or [""])[0]
    local = item.start_utc.astimezone(ZoneInfo(item.timezone))
    return (
        f"Hola {first_name}, le recordamos su cita de {item.service_name} mañana "
        f"{local:%d/%m/%Y} a las {local:%H:%M} en {item.location_name}. Si no puede "
        "asistir, responda este mensaje para reprogramar. ¡Gracias!"
    )


def select_candidates(session: Session, organization_id: int) -> list[Candidate]:
    rows = session.execute(_SELECT, {"org": organization_id, "limit": MAX_CANDIDATES}).all()
    return [Candidate(*row) for row in rows]


def sweep(session: Session, *, caller: ExecutionContext) -> Counts:
    """Select and queue; ``caller`` (the human) is the actor of every message."""
    with session.begin():
        candidates = select_candidates(session, caller.organization_id)
    counts = Counts(candidates=len(candidates))
    for item in candidates:
        if item.conversation_id is None:
            counts.skipped += 1
            continue
        try:
            receipt = enqueue_outbound_message(
                session,
                conversation_id=item.conversation_id,
                text_body=draft_reminder(item),
                idempotency_key=reminder_key(
                    caller.organization_id, item.appointment_id, item.start_utc
                ),
                ctx=caller,
            )
        except AppError as exc:
            if exc.code == ErrorCode.IDEMPOTENCY_KEY_REUSED:
                # Same appointment and start already reminded, through another
                # conversation or with a since-renamed service/location.
                counts.deduped += 1
                continue
            if exc.code not in SKIPPABLE:
                raise
            counts.skipped += 1
            continue
        if receipt.duplicate:
            counts.deduped += 1
        else:
            counts.proposed += 1
    return counts
