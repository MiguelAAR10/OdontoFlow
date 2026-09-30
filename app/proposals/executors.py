"""The ``kind -> executor`` registry (B2 spec, "Registry").

Each kind declares its args model, the permission a human needs to approve it,
its TTL, how to resolve its subject at create time, how to recompute the
subject version at approval time, and how to execute the existing domain
command under the approving human's context. C2/C3 add kinds here additively.

Invariant: no kind ever requires an L4 permission (``payments.reverse``,
``payments.manage``); ``tests/test_agent_proposals.py`` pins it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.clinical.models import Patient, ServiceExecution, Visit
from app.economics.models import Charge, ChargeFollowUp
from app.economics.schemas import ChargeFollowUpCreate
from app.economics.service import OP_FOLLOW_UPS_CREATE, _net_paid_expr, open_follow_up
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import DELIVERIES_CREATE, FOLLOW_UPS_CREATE
from app.idempotency.service import command_fingerprint, run_idempotent_command
from app.messaging.models import ContactIdentity, Conversation
from app.messaging.service import enqueue_outbound_message

TTL = timedelta(hours=72)
CHARGE_SUBJECT = "charge"


# --- args ---------------------------------------------------------------------


class CollectionReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: int = Field(gt=0)
    message_text: str = Field(min_length=1, max_length=1000)


class CollectionFollowUpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_id: int = Field(gt=0)
    next_follow_up_on: date
    note: str | None = Field(default=None, min_length=1, max_length=500)


# --- charge facts (server-computed; never the agent's evidence) ---------------


@dataclass(frozen=True, slots=True)
class ChargeFacts:
    charge_id: int
    amount: Decimal
    paid: Decimal
    patient_id: int
    patient_name: str
    location_id: int
    open_follow_up_id: int | None

    @property
    def balance(self) -> Decimal:
        return self.amount - self.paid

    def as_json(self) -> dict:
        return {
            "charge_id": self.charge_id,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "amount": f"{self.amount:.2f}",
            "paid": f"{self.paid:.2f}",
            "balance": f"{self.balance:.2f}",
        }


def charge_facts(session: Session, organization_id: int, charge_ids) -> dict[int, ChargeFacts]:
    """Amount, net paid, patient and location (charge → execution → visit), org-scoped."""
    ids = list({int(i) for i in charge_ids})
    if not ids:
        return {}
    open_follow_up_id = (
        select(ChargeFollowUp.id)
        .where(
            ChargeFollowUp.organization_id == Charge.organization_id,
            ChargeFollowUp.charge_id == Charge.id,
            ChargeFollowUp.state == "open",
        )
        .correlate(Charge)
        .limit(1)
        .scalar_subquery()
    )
    rows = session.execute(
        select(
            Charge.id,
            Charge.amount,
            _net_paid_expr(),
            Patient.id,
            Patient.full_name,
            Visit.location_id,
            open_follow_up_id,
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
        .where(Charge.organization_id == organization_id, Charge.id.in_(ids))
    ).all()
    return {
        row[0]: ChargeFacts(
            charge_id=row[0],
            amount=Decimal(row[1]),
            paid=Decimal(row[2] or 0),
            patient_id=row[3],
            patient_name=row[4],
            location_id=row[5],
            open_follow_up_id=row[6],
        )
        for row in rows
    }


def _charge(session: Session, organization_id: int, charge_id: int) -> ChargeFacts:
    facts = charge_facts(session, organization_id, [charge_id]).get(charge_id)
    if facts is None:
        raise AppError(ErrorCode.NOT_FOUND, "Charge not found.")
    return facts


def require_open_balance(session: Session, organization_id: int, args: BaseModel) -> None:
    """Create-time and tx1 re-validation shared by both collection kinds."""
    if _charge(session, organization_id, args.charge_id).balance <= 0:
        raise AppError(ErrorCode.INVALID_INPUT, "The charge is already fully paid.")


def reachable_conversation(session: Session, organization_id: int, patient_id: int) -> int | None:
    """The patient's latest open conversation whose contact has not opted out."""
    return session.scalar(
        select(Conversation.id)
        .join(
            ContactIdentity,
            and_(
                ContactIdentity.organization_id == Conversation.organization_id,
                ContactIdentity.id == Conversation.contact_identity_id,
            ),
        )
        .where(
            Conversation.organization_id == organization_id,
            ContactIdentity.patient_id == patient_id,
            ContactIdentity.consent_status != "opted_out",
            Conversation.status != "closed",
        )
        .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
        .limit(1)
    )


# --- registry -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subject:
    subject_type: str
    subject_id: str
    subject_version: str
    location_id: int | None
    conversation_id: int | None


@dataclass(frozen=True, slots=True)
class Execution:
    """What an executor needs; detached from the ORM row."""

    proposal_id: int
    execution_key: UUID
    conversation_id: int | None
    args: BaseModel


@dataclass(frozen=True, slots=True)
class ProposalKind:
    name: str
    args_model: type[BaseModel]
    required_permission: str
    ttl: timedelta
    subject: Callable[[Session, int, BaseModel], Subject]
    version: Callable[[Session, int, BaseModel], str]
    execute: Callable[[Session, ExecutionContext, Execution], dict]
    summary: Callable[[BaseModel, ChargeFacts | None], str]


def _money(value: Decimal) -> str:
    return f"S/ {value:.2f}"


# collection_follow_up ----------------------------------------------------------


def _follow_up_version(facts: ChargeFacts | None) -> str:
    if facts is None:
        return "-"
    return f"{facts.paid:.2f}|{facts.open_follow_up_id or '-'}"


def _follow_up_subject(session: Session, organization_id: int, args) -> Subject:
    facts = _charge(session, organization_id, args.charge_id)
    if facts.balance <= 0:
        raise AppError(ErrorCode.INVALID_INPUT, "The charge is already fully paid.")
    if facts.open_follow_up_id is not None:
        raise AppError(ErrorCode.INVALID_INPUT, "The charge already has an open follow-up.")
    return Subject(CHARGE_SUBJECT, str(facts.charge_id), _follow_up_version(facts),
                   facts.location_id, None)


def _follow_up_current(session: Session, organization_id: int, args) -> str:
    return _follow_up_version(charge_facts(session, organization_id, [args.charge_id]).get(args.charge_id))


def _follow_up_execute(session: Session, ctx: ExecutionContext, item: Execution) -> dict:
    args = item.args
    data = ChargeFollowUpCreate(next_follow_up_on=args.next_follow_up_on, note=args.note)
    outcome = run_idempotent_command(
        session,
        operation=open_follow_up,
        operation_name=OP_FOLLOW_UPS_CREATE,
        key=str(item.execution_key),
        ctx=ctx,
        params={"charge_id": args.charge_id, **data.model_dump()},
        charge_id=args.charge_id,
        data=data,
    )
    if outcome.replayed:
        return {"type": "charge_follow_up", "id": int(outcome.outcome["resource_id"])}
    return {"type": "charge_follow_up", "id": outcome.result.id}


def _follow_up_summary(args, facts: ChargeFacts | None) -> str:
    when = args.next_follow_up_on.strftime("%d/%m/%Y")
    if facts is None:
        return f"Seguimiento de cobro — contactar el {when}"
    return (
        f"Seguimiento de cobro a {facts.patient_name} — saldo {_money(facts.balance)}, "
        f"contactar el {when}"
    )


# collection_reminder -------------------------------------------------------------


def _reminder_version(facts: ChargeFacts | None) -> str:
    # Net paid only: a follow-up opened meanwhile must not supersede a reminder.
    return "-" if facts is None else f"{facts.paid:.2f}"


def _reminder_subject(session: Session, organization_id: int, args) -> Subject:
    facts = _charge(session, organization_id, args.charge_id)
    if facts.balance <= 0:
        raise AppError(ErrorCode.INVALID_INPUT, "The charge is already fully paid.")
    conversation_id = reachable_conversation(session, organization_id, facts.patient_id)
    if conversation_id is None:
        raise AppError(ErrorCode.NOT_FOUND, "The patient has no reachable conversation.")
    return Subject(CHARGE_SUBJECT, str(facts.charge_id), _reminder_version(facts),
                   facts.location_id, conversation_id)


def _reminder_current(session: Session, organization_id: int, args) -> str:
    return _reminder_version(charge_facts(session, organization_id, [args.charge_id]).get(args.charge_id))


def _reminder_execute(session: Session, ctx: ExecutionContext, item: Execution) -> dict:
    with session.begin():
        consent = session.scalar(
            select(ContactIdentity.consent_status)
            .join(
                Conversation,
                and_(
                    Conversation.organization_id == ContactIdentity.organization_id,
                    Conversation.contact_identity_id == ContactIdentity.id,
                ),
            )
            .where(
                Conversation.organization_id == ctx.organization_id,
                Conversation.id == item.conversation_id,
            )
        )
        if consent is None or consent == "opted_out":
            raise AppError(ErrorCode.NOT_FOUND, "The patient has no reachable conversation.")
    receipt = enqueue_outbound_message(
        session,
        conversation_id=item.conversation_id,
        text_body=item.args.message_text,
        idempotency_key=str(item.execution_key),
        ctx=ctx,
    )
    return {"type": "outbound_message", "id": receipt.outbound_id}


def _reminder_summary(args, facts: ChargeFacts | None) -> str:
    if facts is None:
        return "Recordatorio de pago"
    return f"Recordatorio de pago a {facts.patient_name} — saldo {_money(facts.balance)}"


KINDS: dict[str, ProposalKind] = {
    "collection_reminder": ProposalKind(
        name="collection_reminder",
        args_model=CollectionReminderArgs,
        required_permission=DELIVERIES_CREATE,
        ttl=TTL,
        subject=_reminder_subject,
        version=_reminder_current,
        execute=_reminder_execute,
        summary=_reminder_summary,
    ),
    "collection_follow_up": ProposalKind(
        name="collection_follow_up",
        args_model=CollectionFollowUpArgs,
        required_permission=FOLLOW_UPS_CREATE,
        ttl=TTL,
        subject=_follow_up_subject,
        version=_follow_up_current,
        execute=_follow_up_execute,
        summary=_follow_up_summary,
    ),
}


def normalize_payload(kind: str, raw: dict) -> tuple[BaseModel, dict]:
    """Validate the agent's payload; the stored payload is the normalized args."""
    spec = KINDS.get(kind)
    if spec is None:
        raise AppError(ErrorCode.INVALID_INPUT, "Unknown proposal kind.")
    try:
        args = spec.args_model.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in error["loc"]) for error in exc.errors()})
        raise AppError(
            ErrorCode.INVALID_INPUT, "The proposal payload is invalid.", details={"fields": fields}
        ) from exc
    return args, args.model_dump(mode="json", exclude_none=True)


def payload_hash(kind: str, organization_id: int, payload: dict) -> str:
    return command_fingerprint(operation=kind, organization_id=organization_id, params=payload)


def load_args(kind: str, payload: dict) -> BaseModel:
    return KINDS[kind].args_model.model_validate(payload)
