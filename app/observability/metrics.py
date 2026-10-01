"""B3 productivity report: one SQL per block, computed on the fly.

Spec: ``docs/superpowers/specs/2026-10-01-erp-b3.md`` (*Metrics*). Dates are
inclusive and local to each row's location timezone. Proposals expire lazily
(no sweep), so counts use the *effective* status: a ``pending`` row past
``expires_at`` is expired. A declined appointment proposal is stored as
``expired``; only its ``appointment_proposal.declined`` audit row tells it apart.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import AUDIT_READ
from app.iam.service import require_permission
from app.observability.common import require_human
from app.observability.schemas import (
    AgentProposalCounts,
    AppointmentCounts,
    MoneyTotals,
    ProductivityReport,
)

MAX_SPAN_DAYS = 92
DEFAULT_TZ = "America/Lima"
ALWAYS_LISTED = ("cobranza", "reception")

_APPOINTMENTS = """
SELECT a.state, count(*) AS n
  FROM appointments a
  JOIN locations l ON l.organization_id = a.organization_id AND l.id = a.location_id
 WHERE a.organization_id = :org
   AND a.state IN ('completed', 'no_show', 'cancelled')
   AND (a.start_utc AT TIME ZONE l.timezone)::date BETWEEN :start AND :end
   {loc}
 GROUP BY a.state
"""

_MONEY = """
WITH scoped AS (
    SELECT c.id, c.amount, c.created_at, l.timezone
      FROM charges c
      JOIN service_executions se
        ON se.organization_id = c.organization_id AND se.id = c.service_execution_id
      JOIN visits v ON v.organization_id = se.organization_id AND v.id = se.visit_id
      JOIN locations l ON l.organization_id = v.organization_id AND l.id = v.location_id
     WHERE c.organization_id = :org {loc}
), live AS (
    SELECT p.charge_id, p.amount, p.paid_at
      FROM payments p
     WHERE p.organization_id = :org
       AND NOT EXISTS (
           SELECT 1 FROM payment_reversals r
            WHERE r.organization_id = p.organization_id AND r.payment_id = p.id)
), cohort AS (
    SELECT s.* FROM scoped s
     WHERE (s.created_at AT TIME ZONE s.timezone)::date BETWEEN :start AND :end
)
SELECT
    (SELECT coalesce(sum(amount), 0) FROM cohort) AS charged,
    (SELECT coalesce(sum(lv.amount), 0)
       FROM live lv JOIN scoped s ON s.id = lv.charge_id
      WHERE (lv.paid_at AT TIME ZONE s.timezone)::date BETWEEN :start AND :end) AS collected,
    (SELECT coalesce(sum(c.amount - coalesce(
                (SELECT sum(lv.amount) FROM live lv WHERE lv.charge_id = c.id), 0)), 0)
       FROM cohort c) AS outstanding
"""

_AGENT_PROPOSALS = """
SELECT p.agent_key,
       count(*) AS created,
       count(*) FILTER (WHERE p.decided_by_principal_id IS NOT NULL
                          AND p.status <> 'declined') AS approved,
       count(*) FILTER (WHERE p.status = 'declined') AS declined,
       count(*) FILTER (WHERE p.status = 'expired'
                           OR (p.status = 'pending' AND p.expires_at <= :now)) AS expired,
       count(*) FILTER (WHERE p.kind = 'collection_reminder'
                          AND p.decided_by_principal_id IS NOT NULL
                          AND p.status <> 'declined') AS reminders
  FROM agent_proposals p
  LEFT JOIN locations l ON l.organization_id = p.organization_id AND l.id = p.location_id
 WHERE p.organization_id = :org
   AND (p.created_at AT TIME ZONE coalesce(l.timezone, :default_tz))::date
       BETWEEN :start AND :end
   {loc}
 GROUP BY p.agent_key
"""

_APPOINTMENT_PROPOSALS = """
SELECT count(*) AS created,
       count(*) FILTER (WHERE x.status = 'confirmed') AS approved,
       count(*) FILTER (WHERE x.declined) AS declined,
       count(*) FILTER (WHERE (x.status = 'expired' AND NOT x.declined)
                           OR (x.status = 'pending' AND x.expires_at <= :now)) AS expired
  FROM (
    SELECT ap.status, ap.expires_at,
           EXISTS (SELECT 1 FROM audit_events e
                    WHERE e.organization_id = ap.organization_id
                      AND e.entity_type = 'appointment_proposal'
                      AND e.entity_id = ap.id::text
                      AND e.action = 'appointment_proposal.declined') AS declined
      FROM appointment_proposals ap
      JOIN locations l ON l.organization_id = ap.organization_id AND l.id = ap.location_id
     WHERE ap.organization_id = :org
       AND (ap.created_at AT TIME ZONE l.timezone)::date BETWEEN :start AND :end
       {loc}
  ) x
"""


def _money(value) -> str:
    return f"{Decimal(value or 0):.2f}"


def validate_range(start: date, end: date) -> None:
    if start > end:
        raise AppError(ErrorCode.INVALID_INPUT, "from must not be after to.")
    if (end - start).days > MAX_SPAN_DAYS:
        raise AppError(ErrorCode.INVALID_INPUT, f"The range spans more than {MAX_SPAN_DAYS} days.")


def productivity(
    session: Session,
    *,
    ctx: ExecutionContext,
    start: date,
    end: date,
    location_id: int | None = None,
) -> ProductivityReport:
    validate_range(start, end)
    params = {
        "org": ctx.organization_id,
        "start": start,
        "end": end,
        "now": datetime.now(timezone.utc),
        "default_tz": DEFAULT_TZ,
    }
    if location_id is not None:
        params["location_id"] = location_id

    def sql(template: str, column: str) -> str:
        loc = f"AND {column} = :location_id" if location_id is not None else ""
        return template.format(loc=loc)

    with session.begin():
        require_human(ctx)
        require_permission(session, ctx, AUDIT_READ, location_id=location_id)
        states = dict(session.execute(text(sql(_APPOINTMENTS, "a.location_id")), params).all())
        money = session.execute(text(sql(_MONEY, "v.location_id")), params).one()
        agent_rows = session.execute(
            text(sql(_AGENT_PROPOSALS, "p.location_id")), params
        ).all()
        reception = session.execute(
            text(sql(_APPOINTMENT_PROPOSALS, "ap.location_id")), params
        ).one()

    buckets = {key: [0, 0, 0, 0] for key in ALWAYS_LISTED}
    reminders = 0
    for row in agent_rows:
        bucket = buckets.setdefault(row.agent_key, [0, 0, 0, 0])
        for i, value in enumerate((row.created, row.approved, row.declined, row.expired)):
            bucket[i] += value
        reminders += row.reminders
    for i, value in enumerate(
        (reception.created, reception.approved, reception.declined, reception.expired)
    ):
        buckets["reception"][i] += value
    return ProductivityReport(
        from_=start,
        to=end,
        location_id=location_id,
        appointments=AppointmentCounts(
            completed=states.get("completed", 0),
            no_show=states.get("no_show", 0),
            cancelled=states.get("cancelled", 0),
        ),
        money=MoneyTotals(
            charged=_money(money.charged),
            collected=_money(money.collected),
            outstanding=_money(money.outstanding),
        ),
        proposals=[
            AgentProposalCounts(
                agent_key=key, created=v[0], approved=v[1], declined=v[2], expired=v[3]
            )
            for key, v in sorted(buckets.items())
        ],
        collection_reminders_approved=reminders,
    )
