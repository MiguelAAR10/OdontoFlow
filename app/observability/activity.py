"""B3 activity feed: ``audit ∪ proposal ∪ agent_run`` in one keyset-paged SQL.

Spec: ``docs/superpowers/specs/2026-10-01-erp-b3.md`` (*Activity*). Three
disjoint branches, each ``organization_id``-scoped. ``audit_events.actor_id``
is text (``'system'`` for system rows), so principals join on
``principals.id::text`` and never cast the audit column to int. ``summary`` is
built from ``action`` and the actor name only — never from state JSON.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session
from sqlalchemy.types import DateTime, Integer, String

from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import PROPOSALS_READ
from app.iam.service import require_permission
from app.observability.common import decode_cursor, encode_cursor, require_human
from app.observability.schemas import ActivityItem, ActivityPage

SOURCES = ("audit", "proposal", "agent_run")
SYSTEM_DISPLAY_NAME = "Sistema"

#: Closed Spanish template map: ``"{actor} {label}"``. Unknown action → its code.
LABELS: dict[str, str] = {
    "agent_proposal.created": "propuso una acción para aprobar",
    "agent_proposal.approved": "aprobó una propuesta",
    "agent_proposal.declined": "rechazó una propuesta",
    "agent_proposal.executed": "ejecutó una propuesta aprobada",
    "agent_proposal.failed": "registró una propuesta fallida",
    "agent_proposal.expired": "registró una propuesta vencida",
    "agent_proposal.superseded": "registró una propuesta desactualizada",
    "agent_run.running": "inició una corrida",
    "agent_run.completed": "completó una corrida",
    "agent_run.failed": "registró una corrida fallida",
    "agent_tool.called": "usó una herramienta",
    "appointment.created": "agendó una cita",
    "appointment.cancelled": "canceló una cita",
    "appointment.rescheduled": "reprogramó una cita",
    "appointment.completed": "marcó una cita como atendida",
    "appointment.no_show": "marcó una inasistencia",
    "appointment_proposal.created": "propuso una cita",
    "appointment_proposal.confirmed": "confirmó una cita propuesta",
    "appointment_proposal.declined": "rechazó una cita propuesta",
    "appointment_cancellation_proposal.created": "propuso cancelar una cita",
    "appointment_reschedule_proposal.created": "propuso reprogramar una cita",
    "charge.created": "registró un cobro",
    "charge_follow_up.opened": "abrió un seguimiento de cobranza",
    "charge_follow_up.rescheduled": "reprogramó un seguimiento de cobranza",
    "charge_follow_up.closed": "cerró un seguimiento de cobranza",
    "contact_profile.registered": "registró los datos de un contacto",
    "conversation.automation_resumed": "devolvió la conversación a AIRY",
    "conversation.closed": "cerró una conversación",
    "conversation.human_handoff_requested": "derivó una conversación a una persona",
    "message.received": "recibió un mensaje",
    "outbound.queued": "encoló un mensaje",
    "outbound.settled": "registró la entrega de un mensaje",
    "outbound.dead_lettered": "registró un mensaje no entregado",
    "patient.created": "registró un paciente",
    "payment.created": "registró un pago",
    "payment.verified": "verificó un pago",
    "payment.reversed": "anuló un pago",
    "reception_handoff.claimed": "tomó una derivación",
    "service_execution.created": "registró un servicio realizado",
    "visit.created": "registró una visita",
    "waitlist_entry.created": "agregó a la lista de espera",
    "waitlist_entry.cancelled": "retiró de la lista de espera",
}

_FEED = """
WITH feed AS (
    SELECT 'audit'::text AS source, a.id, a.occurred_at, a.action::text AS action,
           a.entity_type::text AS entity_type, a.entity_id::text AS entity_id,
           a.actor_type::text AS actor_kind, a.actor_id::text AS actor_ref,
           NULL::text AS agent_key, ap.location_id AS location_id
      FROM audit_events a
      LEFT JOIN appointments ap
        ON a.entity_type = 'appointment'
       AND ap.organization_id = a.organization_id
       AND ap.id::text = a.entity_id
     WHERE a.organization_id = :org
       AND a.entity_type NOT IN ('agent_proposal', 'appointment_proposal', 'agent_run')
    UNION ALL
    SELECT 'proposal'::text, a.id, a.occurred_at, a.action::text, a.entity_type::text,
           a.entity_id::text, a.actor_type::text, a.actor_id::text,
           CASE WHEN a.entity_type = 'appointment_proposal' THEN 'reception'
                ELSE p.agent_key END,
           COALESCE(p.location_id, apr.location_id)
      FROM audit_events a
      LEFT JOIN agent_proposals p
        ON a.entity_type = 'agent_proposal'
       AND p.organization_id = a.organization_id
       AND p.id::text = a.entity_id
      LEFT JOIN appointment_proposals apr
        ON a.entity_type = 'appointment_proposal'
       AND apr.organization_id = a.organization_id
       AND apr.id::text = a.entity_id
     WHERE a.organization_id = :org
       AND a.entity_type IN ('agent_proposal', 'appointment_proposal')
    UNION ALL
    SELECT 'agent_run'::text, r.id, r.started_at, 'agent_run.' || r.status, 'agent_run'::text,
           r.id::text, pr.type::text, r.triggered_by_principal_id::text,
           r.agent_key, NULL::integer
      FROM agent_runs r
      JOIN principals pr ON pr.id = r.triggered_by_principal_id
     WHERE r.organization_id = :org
)
SELECT f.source, f.id, f.occurred_at, f.action, f.entity_type, f.entity_id, f.actor_kind,
       f.agent_key, f.location_id, pn.id AS actor_principal_id, pn.display_name AS actor_name
  FROM feed f
  LEFT JOIN principals pn ON pn.id::text = f.actor_ref
"""


def summarize(actor: str, action: str) -> str:
    label = LABELS.get(action)
    return f"{actor} {label}" if label else f"{actor}: {action}"


def list_activity(
    session: Session,
    *,
    ctx: ExecutionContext,
    location_id: int | None = None,
    agent_key: str | None = None,
    since: datetime | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> ActivityPage:
    after = decode_cursor(cursor, (datetime, str, int)) if cursor else None
    if after is not None and after[1] not in SOURCES:
        raise AppError(ErrorCode.INVALID_INPUT, "The cursor is invalid.")
    if since is not None and since.tzinfo is None:
        raise AppError(ErrorCode.INVALID_INPUT, "since must be timezone-aware.")
    where, params = [], {"org": ctx.organization_id, "limit": limit + 1}
    binds = [bindparam("org", type_=Integer), bindparam("limit", type_=Integer)]
    if agent_key is not None:
        where.append("f.agent_key = :agent_key")
        params["agent_key"] = agent_key
        binds.append(bindparam("agent_key", type_=String))
    if location_id is not None:
        where.append("f.location_id = :location_id")
        params["location_id"] = location_id
        binds.append(bindparam("location_id", type_=Integer))
    if since is not None:
        where.append("f.occurred_at >= :since")
        params["since"] = since
        binds.append(bindparam("since", type_=DateTime(timezone=True)))
    if after is not None:
        where.append("(f.occurred_at, f.source, f.id) < (:c_at, :c_source, :c_id)")
        params.update(c_at=after[0], c_source=after[1], c_id=after[2])
        binds += [
            bindparam("c_at", type_=DateTime(timezone=True)),
            bindparam("c_source", type_=String),
            bindparam("c_id", type_=Integer),
        ]
    sql = _FEED
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.occurred_at DESC, f.source DESC, f.id DESC LIMIT :limit"
    with session.begin():
        require_human(ctx)
        require_permission(session, ctx, PROPOSALS_READ)
        rows = session.execute(text(sql).bindparams(*binds), params).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        system = row.actor_kind == "system"
        name = SYSTEM_DISPLAY_NAME if system or row.actor_name is None else row.actor_name
        items.append(
            ActivityItem(
                source=row.source,
                id=row.id,
                occurred_at=row.occurred_at,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                actor_kind=row.actor_kind,
                actor_principal_id=row.actor_principal_id,
                actor_display_name=name,
                agent_key=row.agent_key,
                location_id=row.location_id,
                summary=summarize(name, row.action),
            )
        )
    last = rows[-1] if rows else None
    return ActivityPage(
        items=items,
        next_cursor=encode_cursor(last.occurred_at, last.source, last.id) if has_more else None,
    )
