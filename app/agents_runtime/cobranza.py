"""The collections sweep (COB): SQL picks the work, a fixed template drafts.

One org-scoped query selects overdue charges; each candidate becomes at most
one ``collection_reminder`` proposal through B2's ``create_proposal``. The
sweep never approves or executes anything — a human does, in B2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Text, and_, cast, exists, func, select, true
from sqlalchemy.orm import Session

from app.clinical.models import Patient, ServiceExecution, Visit
from app.economics.models import Charge, Payment
from app.economics.service import _net_paid_expr, _payment_not_reversed
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.organization.models import Location
from app.proposals.executors import CHARGE_SUBJECT, normalize_payload
from app.proposals.models import AgentProposal
from app.proposals.service import create_proposal

AGENT_KEY = "cobranza"
KIND = "collection_reminder"
#: A charge is overdue once it still has a balance this many local days after
#: it was issued (no due-date column; same threshold as the demo seed).
OVERDUE_MIN_AGE_DAYS = 7
MAX_CANDIDATES = 200
#: Only "paid meanwhile" and "unreachable / opted out" are expected per-charge
#: outcomes; any other error (e.g. PERMISSION_DENIED) fails the run.
SKIPPABLE = (ErrorCode.INVALID_INPUT, ErrorCode.NOT_FOUND)


@dataclass(frozen=True, slots=True)
class Candidate:
    charge_id: int
    amount: Decimal
    paid: Decimal
    issued_on: date
    days_since_issued: int
    patient_full_name: str
    location_name: str
    proposed_today: bool
    last_payment_at: datetime | None
    last_payment_amount: Decimal | None

    @property
    def balance(self) -> Decimal:
        return self.amount - self.paid


@dataclass(frozen=True, slots=True)
class Draft:
    message_text: str
    reason: str


@dataclass(slots=True)
class Counts:
    candidates: int = 0
    proposed: int = 0
    deduped: int = 0
    skipped: int = 0

    def as_json(self) -> dict:
        return {
            "candidates": self.candidates,
            "proposed": self.proposed,
            "deduped": self.deduped,
            "skipped": self.skipped,
        }


def draft_reminder(
    *,
    full_name: str,
    location_name: str,
    balance: Decimal,
    issued_on: date,
    days_since_issued: int,
) -> Draft:
    """The fixed Spanish template; pure (no Session, no LLM)."""
    first_name = (full_name.split() or [""])[0]
    amount = f"S/ {balance:.2f}"
    return Draft(
        message_text=(
            f"Hola {first_name}, le escribimos de {location_name}. Tiene un saldo pendiente "
            f"de {amount} por su atención del {issued_on:%d/%m/%Y}. Puede pagarlo en la sede "
            "o responder este mensaje para coordinar. ¡Gracias!"
        ),
        reason=f"Saldo vencido de {amount} hace {days_since_issued} días",
    )


def select_candidates(session: Session, organization_id: int) -> list[Candidate]:
    """The one selection query: balance > 0 and local age ≥ 7 days, org-scoped."""
    tz = Location.timezone
    today = cast(func.timezone(tz, func.now()), Date)
    issued_on = cast(func.timezone(tz, Charge.created_at), Date)
    age = today - issued_on
    net_paid = _net_paid_expr()
    proposed_today = exists().where(
        AgentProposal.organization_id == Charge.organization_id,
        AgentProposal.kind == KIND,
        AgentProposal.subject_type == CHARGE_SUBJECT,
        AgentProposal.subject_id == cast(Charge.id, Text),
        cast(func.timezone(tz, AgentProposal.created_at), Date) == today,
    )
    last_payment = (
        select(Payment.paid_at, Payment.amount)
        .where(
            Payment.organization_id == Charge.organization_id,
            Payment.charge_id == Charge.id,
            _payment_not_reversed(),
        )
        .order_by(Payment.paid_at.desc(), Payment.id.desc())
        .limit(1)
        .lateral("last_payment")
    )
    rows = session.execute(
        select(
            Charge.id,
            Charge.amount,
            net_paid,
            issued_on,
            age,
            Patient.full_name,
            Location.name,
            proposed_today,
            last_payment.c.paid_at,
            last_payment.c.amount,
        )
        .join(
            ServiceExecution,
            and_(
                ServiceExecution.organization_id == Charge.organization_id,
                ServiceExecution.id == Charge.service_execution_id,
            ),
        )
        .join(
            Visit,
            and_(
                Visit.organization_id == ServiceExecution.organization_id,
                Visit.id == ServiceExecution.visit_id,
            ),
        )
        .join(
            Patient,
            and_(Patient.organization_id == Visit.organization_id, Patient.id == Visit.patient_id),
        )
        .join(
            Location,
            and_(Location.organization_id == Visit.organization_id, Location.id == Visit.location_id),
        )
        .outerjoin(last_payment, true())
        .where(
            Charge.organization_id == organization_id,
            Charge.amount - net_paid > 0,
            age >= OVERDUE_MIN_AGE_DAYS,
        )
        .order_by(Charge.id)
        .limit(MAX_CANDIDATES)
    ).all()
    return [
        Candidate(
            charge_id=row[0],
            amount=Decimal(row[1]),
            paid=Decimal(row[2] or 0),
            issued_on=row[3],
            days_since_issued=int(row[4]),
            patient_full_name=row[5],
            location_name=row[6],
            proposed_today=bool(row[7]),
            last_payment_at=row[8],
            last_payment_amount=None if row[9] is None else Decimal(row[9]),
        )
        for row in rows
    ]


def _evidence(run_id: int, item: Candidate) -> dict:
    return {
        "run_id": run_id,
        "amount": f"{item.amount:.2f}",
        "balance": f"{item.balance:.2f}",
        "days_since_issued": item.days_since_issued,
        "issued_on": item.issued_on.isoformat(),
        "last_payment_at": item.last_payment_at.isoformat() if item.last_payment_at else None,
        "last_payment_amount": (
            None if item.last_payment_amount is None else f"{item.last_payment_amount:.2f}"
        ),
    }


def sweep(session: Session, *, run_id: int, proposer: ExecutionContext) -> Counts:
    """Select, draft and propose; ``proposer`` is only ever passed to create_proposal."""
    with session.begin():
        candidates = select_candidates(session, proposer.organization_id)
    counts = Counts(candidates=len(candidates))
    for item in candidates:
        if item.proposed_today:
            counts.deduped += 1
            continue
        draft = draft_reminder(
            full_name=item.patient_full_name,
            location_name=item.location_name,
            balance=item.balance,
            issued_on=item.issued_on,
            days_since_issued=item.days_since_issued,
        )
        args, payload = normalize_payload(
            KIND, {"charge_id": item.charge_id, "message_text": draft.message_text}
        )
        try:
            _proposal, created = create_proposal(
                session,
                ctx=proposer,
                kind=KIND,
                args=args,
                payload=payload,
                reason=draft.reason,
                evidence=_evidence(run_id, item),
                agent_key=AGENT_KEY,
            )
        except AppError as exc:
            if exc.code not in SKIPPABLE:
                raise
            counts.skipped += 1
            continue
        if created:
            counts.proposed += 1
        else:
            counts.deduped += 1
    return counts
