"""B2 application services: create, approve (tx1 → execute → tx3), decline, inbox.

Spec: ``docs/superpowers/specs/2026-10-01-erp-b2.md``. Every mutation owns its
transaction and stages its audit row inside it; reads never write (a pending
row past ``expires_at`` is *reported* as expired and only persisted as expired
by approve, decline or the create sweep).
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import (
    DateTime,
    Integer,
    Text,
    and_,
    case,
    literal,
    literal_column,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_tools.registry import resolve_agent_key
from app.audit.service import record_event
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.models import Principal
from app.iam.permissions import (
    APPOINTMENTS_READ,
    CONTACT_APPOINTMENTS_BOOK,
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
from app.organization.models import Location
from app.proposals.errors import ProposalErrorCode, error_for_status, proposal_error
from app.proposals.executors import (
    KINDS,
    Execution,
    charge_facts,
    load_args,
    normalize_payload,
)
from app.proposals.executors import (
    payload_hash as compute_payload_hash,
)
from app.proposals.models import DEDUPE_INDEX, OPEN_STATUSES, AgentProposal
from app.proposals.schemas import DecidedBy, InboxItem, InboxPage, SubjectRef
from app.scheduling.models import AppointmentProposal

UTC = timezone.utc
OP_PROPOSAL_CREATE = "agent_proposal.create"
OP_PROPOSAL_APPROVE = "agent_proposal.approve"
ENTITY_TYPE = "agent_proposal"
AGENT_SOURCE = "agent_proposal"
APPOINTMENT_SOURCE = "appointment_proposal"
APPOINTMENT_KIND = "appointment_booking"
PROPOSER_TYPES = ("agent", "integration")


def _now() -> datetime:
    return datetime.now(UTC)


def _deny() -> AppError:
    return AppError(
        IamErrorCode.PERMISSION_DENIED,
        PERMISSION_DENIED_MESSAGE,
        details={},
        http_status=PERMISSION_DENIED_HTTP_STATUS,
    )


def _audit(session: Session, ctx: ExecutionContext, proposal: AgentProposal, action: str,
           before: str | None) -> None:
    record_event(
        session,
        ctx=ctx,
        entity_type=ENTITY_TYPE,
        entity_id=str(proposal.id),
        action=action,
        before_state={"status": before} if before else None,
        after_state={
            "status": proposal.status,
            "kind": proposal.kind,
            "payload_hash": proposal.payload_hash,
            "result_ref": proposal.result_ref,
            "error_code": proposal.error_code,
        },
    )


def _transition(session, ctx, proposal: AgentProposal, status: str, **fields) -> None:
    before = proposal.status
    proposal.status = status
    proposal.updated_at = _now()
    for name, value in fields.items():
        setattr(proposal, name, value)
    session.flush()
    _audit(session, ctx, proposal, f"agent_proposal.{status}", before)


def _is_dedupe_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return (
        str(sqlstate) == "23505"
        and diag is not None
        and getattr(diag, "constraint_name", None) == DEDUPE_INDEX
    )


def _open_row(session: Session, organization_id: int, dedupe_key: str) -> AgentProposal | None:
    return session.scalar(
        select(AgentProposal).where(
            AgentProposal.organization_id == organization_id,
            AgentProposal.dedupe_key == dedupe_key,
            AgentProposal.status.in_(OPEN_STATUSES),
        )
    )


def _settle_create(receipt, proposal: AgentProposal, created: bool) -> None:
    settle_receipt(
        receipt,
        resource_type=ENTITY_TYPE,
        resource_id=str(proposal.id),
        outcome_json={"proposal_id": proposal.id, "created": created},
    )


# --- create -------------------------------------------------------------------


def create_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    kind: str,
    args,
    payload: dict,
    reason: str,
    evidence: dict | None = None,
    idempotency: IdempotencyClaim | None = None,
    agent_key: str | None = None,
) -> tuple[AgentProposal, bool]:
    """Create (``True``) or dedupe onto the open proposal (``False``).

    ``agent_key`` is a server-only override (COB's sweep passes ``"cobranza"``);
    ``submit_proposal`` never passes it, so an HTTP body can never set it.
    """
    spec = KINDS[kind]
    org = ctx.organization_id
    try:
        with session.begin():
            receipt = claim_receipt(session, ctx, idempotency)
            if ctx.principal_type not in PROPOSER_TYPES:
                raise _deny()
            require_permission(session, ctx, PROPOSALS_CREATE)
            agent_key = agent_key or resolve_agent_key(session, ctx) or session.scalar(
                select(Principal.display_name).where(Principal.id == ctx.principal_id)
            )
            subject = spec.subject(session, org, args)
            dedupe_key = f"{kind}:{subject.subject_type}:{subject.subject_id}"
            now = _now()
            # An expired row must never hold the dedupe slot (no sweep in B2).
            stale = session.scalars(
                select(AgentProposal)
                .where(
                    AgentProposal.organization_id == org,
                    AgentProposal.dedupe_key == dedupe_key,
                    AgentProposal.status == "pending",
                    AgentProposal.expires_at <= now,
                )
                .with_for_update()
            ).all()
            for row in stale:
                _transition(session, ctx, row, "expired")
            existing = _open_row(session, org, dedupe_key)
            if existing is not None:
                _settle_create(receipt, existing, False)
                return existing, False
            proposal = AgentProposal(
                organization_id=org,
                location_id=subject.location_id,
                conversation_id=subject.conversation_id,
                agent_key=agent_key,
                kind=kind,
                status="pending",
                payload=payload,
                payload_hash=compute_payload_hash(kind, org, payload),
                subject_type=subject.subject_type,
                subject_id=subject.subject_id,
                subject_version=subject.subject_version,
                reason=reason,
                evidence=evidence,
                dedupe_key=dedupe_key,
                execution_key=uuid.uuid4(),
                proposed_by_principal_id=ctx.principal_id,
                correlation_id=ctx.correlation_id,
                created_at=now,
                updated_at=now,
                expires_at=now + spec.ttl,
            )
            session.add(proposal)
            session.flush()
            _audit(session, ctx, proposal, "agent_proposal.created", None)
            _settle_create(receipt, proposal, True)
        return proposal, True
    except IntegrityError as exc:
        if not _is_dedupe_conflict(exc):
            raise
    # A concurrent create won the open slot: re-read it in a fresh transaction.
    session.rollback()
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        existing = _open_row(session, org, dedupe_key)
        if existing is None:
            existing = session.scalar(
                select(AgentProposal)
                .where(AgentProposal.organization_id == org, AgentProposal.dedupe_key == dedupe_key)
                .order_by(AgentProposal.id.desc())
                .limit(1)
            )
        _settle_create(receipt, existing, False)
    return existing, False


@dataclass(frozen=True, slots=True)
class SubmitResult:
    proposal_id: int
    created: bool
    replayed: bool


def submit_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    kind: str,
    payload: dict,
    reason: str,
    evidence: dict | None = None,
    key: str | None,
) -> SubmitResult:
    args, normalized = normalize_payload(kind, payload)
    outcome = run_idempotent_command(
        session,
        operation=create_proposal,
        operation_name=OP_PROPOSAL_CREATE,
        key=key,
        ctx=ctx,
        params={"kind": kind, "payload": normalized, "reason": reason, "evidence": evidence},
        kind=kind,
        args=args,
        payload=normalized,
        reason=reason,
        evidence=evidence,
    )
    if outcome.replayed:
        return SubmitResult(
            int(outcome.outcome["proposal_id"]), bool(outcome.outcome["created"]), True
        )
    proposal, created = outcome.result
    return SubmitResult(proposal.id, created, False)


# --- approve ----------------------------------------------------------------


def _require_human(ctx: ExecutionContext) -> None:
    if ctx.principal_type != "human":
        raise _deny()


def _lock(session: Session, organization_id: int, proposal_id: int) -> AgentProposal:
    proposal = session.scalar(
        select(AgentProposal)
        .where(AgentProposal.organization_id == organization_id, AgentProposal.id == proposal_id)
        .with_for_update()
    )
    if proposal is None:
        raise AppError(ErrorCode.NOT_FOUND, "Proposal not found.")
    return proposal


def approve_proposal(
    session: Session,
    *,
    ctx: ExecutionContext,
    proposal_id: int,
    payload_hash: str,
    note: str | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> AgentProposal:
    """tx1: decide. Any raise rolls back the receipt and leaves the row untouched."""
    org = ctx.organization_id
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        _require_human(ctx)
        require_permission(session, ctx, PROPOSALS_DECIDE)
        proposal = _lock(session, org, proposal_id)
        spec = KINDS[proposal.kind]
        require_permission(session, ctx, spec.required_permission)
        if proposal.status != "pending":
            raise error_for_status(proposal.status)
        if proposal.expires_at <= _now():
            _transition(session, ctx, proposal, "expired")
        elif payload_hash != proposal.payload_hash:
            raise proposal_error(ProposalErrorCode.PROPOSAL_HASH_MISMATCH)
        else:
            args = load_args(proposal.kind, proposal.payload)
            if spec.version(session, org, args) != proposal.subject_version:
                _transition(session, ctx, proposal, "superseded")
            else:
                spec.revalidate(session, org, args)
                _transition(
                    session,
                    ctx,
                    proposal,
                    "approved",
                    decided_by_principal_id=ctx.principal_id,
                    decision_note=note,
                )
        settle_receipt(
            receipt,
            resource_type=ENTITY_TYPE,
            resource_id=str(proposal.id),
            outcome_json={"proposal_id": proposal.id},
        )
    return proposal


def _execute(session: Session, ctx: ExecutionContext, proposal: AgentProposal) -> None:
    """Run the domain command outside tx1, then settle the row in tx3."""
    spec = KINDS[proposal.kind]
    item = Execution(
        proposal_id=proposal.id,
        execution_key=proposal.execution_key,
        conversation_id=proposal.conversation_id,
        args=load_args(proposal.kind, proposal.payload),
    )
    result_ref, error_code = None, None
    try:
        result_ref = spec.execute(session, ctx, item)
    except AppError as exc:
        session.rollback()
        error_code = exc.code.value
    with session.begin():
        row = _lock(session, ctx.organization_id, proposal.id)
        if row.status != "approved":
            return
        if error_code is None:
            _transition(session, ctx, row, "executed", result_ref=result_ref)
        else:
            _transition(session, ctx, row, "failed", error_code=error_code)


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    proposal: AgentProposal
    replayed: bool

    @property
    def status(self) -> str:
        return self.proposal.status


def _reload(session: Session, organization_id: int, proposal_id: int) -> AgentProposal:
    session.expire_all()
    with session.begin():
        proposal = session.scalar(
            select(AgentProposal).where(
                AgentProposal.organization_id == organization_id, AgentProposal.id == proposal_id
            )
        )
    if proposal is None:
        raise AppError(ErrorCode.NOT_FOUND, "Proposal not found.")
    return proposal


def approve_and_execute(
    session: Session,
    *,
    ctx: ExecutionContext,
    proposal_id: int,
    payload_hash: str,
    note: str | None = None,
    key: str | None,
) -> ApprovalResult:
    outcome = run_idempotent_command(
        session,
        operation=approve_proposal,
        operation_name=OP_PROPOSAL_APPROVE,
        key=key,
        ctx=ctx,
        params={"proposal_id": proposal_id, "payload_hash": payload_hash, "note": note},
        proposal_id=proposal_id,
        payload_hash=payload_hash,
        note=note,
    )
    if not outcome.replayed and outcome.result.status == "approved":
        _execute(session, ctx, outcome.result)
    proposal = _reload(session, ctx.organization_id, proposal_id)
    if proposal.status in ("expired", "superseded"):
        raise error_for_status(proposal.status)
    return ApprovalResult(proposal, outcome.replayed)


# --- decline ------------------------------------------------------------------


def decline_proposal(
    session: Session, *, ctx: ExecutionContext, proposal_id: int, note: str | None = None
) -> AgentProposal:
    expired = False
    with session.begin():
        _require_human(ctx)
        require_permission(session, ctx, PROPOSALS_DECIDE)
        proposal = _lock(session, ctx.organization_id, proposal_id)
        if proposal.status == "declined":
            return proposal
        if proposal.status != "pending":
            raise error_for_status(proposal.status)
        if proposal.expires_at <= _now():
            _transition(session, ctx, proposal, "expired")
            expired = True
        else:
            _transition(
                session,
                ctx,
                proposal,
                "declined",
                decided_by_principal_id=ctx.principal_id,
                decision_note=note,
            )
    if expired:
        raise error_for_status("expired")
    return proposal


# --- read side ------------------------------------------------------------------


class _Permissions:
    """Per-request memo of ``has_permission`` answers used to render ``actions``."""

    def __init__(self, session: Session, ctx: ExecutionContext) -> None:
        self._session, self._ctx, self._memo = session, ctx, {}

    def has(self, code: str, location_id: int | None = None) -> bool:
        key = (code, location_id)
        if key not in self._memo:
            self._memo[key] = has_permission(
                self._session, self._ctx.principal_id, self._ctx.organization_id, code, location_id
            )
        return self._memo[key]


def _effective_status(status: str, expires_at: datetime, now: datetime) -> str:
    return "expired" if status == "pending" and expires_at <= now else status


def _agent_items(session, ctx, rows, perms: _Permissions, now) -> dict[int, InboxItem]:
    facts = charge_facts(
        session,
        ctx.organization_id,
        [int(p.subject_id) for p, _name in rows if p.subject_type == "charge"],
    )
    items = {}
    for proposal, decider_name in rows:
        spec = KINDS[proposal.kind]
        status = _effective_status(proposal.status, proposal.expires_at, now)
        charge = facts.get(int(proposal.subject_id)) if proposal.subject_type == "charge" else None
        actions: list[str] = []
        if status == "pending" and ctx.principal_type == "human" and perms.has(PROPOSALS_DECIDE):
            if perms.has(spec.required_permission):
                actions.append("approve")
            actions.append("decline")
        items[proposal.id] = InboxItem(
            source=AGENT_SOURCE,
            id=proposal.id,
            kind=proposal.kind,
            agent_key=proposal.agent_key,
            status=status,
            location_id=proposal.location_id,
            summary=spec.summary(load_args(proposal.kind, proposal.payload), charge),
            reason=proposal.reason,
            facts=charge.as_json() if charge else None,
            evidence=proposal.evidence,
            payload=proposal.payload,
            payload_hash=proposal.payload_hash,
            subject=SubjectRef(type=proposal.subject_type, id=proposal.subject_id),
            conversation_id=proposal.conversation_id,
            confirmation_token=None,
            expires_at=proposal.expires_at,
            created_at=proposal.created_at,
            decided_by=(
                DecidedBy(id=proposal.decided_by_principal_id, display_name=decider_name)
                if proposal.decided_by_principal_id is not None
                else None
            ),
            result_ref=proposal.result_ref,
            error_code=proposal.error_code,
            actions=actions,
        )
    return items


def _appointment_status(status: str, expires_at: datetime, now: datetime) -> str:
    if status == "confirmed":
        return "executed"
    return _effective_status(status, expires_at, now)


def _appointment_items(ctx, rows, perms: _Permissions, now) -> dict[int, InboxItem]:
    items = {}
    for proposal, timezone_name in rows:
        status = _appointment_status(proposal.status, proposal.expires_at, now)
        local = proposal.start_utc.astimezone(ZoneInfo(timezone_name or "America/Lima"))
        actions: list[str] = []
        if (
            status == "pending"
            and ctx.principal_type == "human"
            and perms.has(CONTACT_APPOINTMENTS_BOOK, proposal.location_id)
        ):
            actions = ["approve", "decline"]
        items[proposal.id] = InboxItem(
            source=APPOINTMENT_SOURCE,
            id=proposal.id,
            kind=APPOINTMENT_KIND,
            agent_key=None,
            status=status,
            location_id=proposal.location_id,
            summary=f"Cita para {proposal.full_name} — {local:%d/%m/%Y %H:%M}",
            reason=None,
            facts=None,
            evidence=None,
            payload={
                "lead_id": proposal.lead_id,
                "patient_id": proposal.patient_id,
                "full_name": proposal.full_name,
                "service_id": proposal.service_id,
                "practitioner_id": proposal.practitioner_id,
                "start_utc": proposal.start_utc.isoformat(),
                "end_utc": proposal.end_utc.isoformat(),
            },
            payload_hash=None,
            subject=None,
            conversation_id=proposal.conversation_id,
            confirmation_token=proposal.confirmation_token,
            expires_at=proposal.expires_at,
            created_at=proposal.created_at,
            decided_by=None,
            result_ref=(
                {"type": "appointment", "id": proposal.appointment_id}
                if proposal.appointment_id is not None
                else None
            ),
            error_code=None,
            actions=actions,
        )
    return items


def _load_agent_rows(session: Session, organization_id: int, ids):
    return session.execute(
        select(AgentProposal, Principal.display_name)
        .outerjoin(Principal, Principal.id == AgentProposal.decided_by_principal_id)
        .where(AgentProposal.organization_id == organization_id, AgentProposal.id.in_(list(ids)))
    ).all()


def proposal_item(session: Session, ctx: ExecutionContext, proposal_id: int) -> InboxItem:
    """Render one agent proposal of the caller's organization (no permission check)."""
    rows = _load_agent_rows(session, ctx.organization_id, [proposal_id])
    if not rows:
        raise AppError(ErrorCode.NOT_FOUND, "Proposal not found.")
    return _agent_items(session, ctx, rows, _Permissions(session, ctx), _now())[proposal_id]


def get_proposal_item(session: Session, *, ctx: ExecutionContext, proposal_id: int) -> InboxItem:
    require_permission(session, ctx, PROPOSALS_READ)
    return proposal_item(session, ctx, proposal_id)


def _encode_cursor(created_at: datetime, source: str, item_id: int) -> str:
    raw = json.dumps([created_at.isoformat(), source, item_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, source, item_id = json.loads(base64.urlsafe_b64decode(padded))
        parsed = datetime.fromisoformat(created_at)
        if source not in (AGENT_SOURCE, APPOINTMENT_SOURCE) or parsed.tzinfo is None:
            raise ValueError(source)
        return parsed, source, int(item_id)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AppError(ErrorCode.INVALID_INPUT, "The inbox cursor is invalid.") from exc


def list_inbox(
    session: Session,
    *,
    ctx: ExecutionContext,
    status: str = "pending",
    source: str | None = None,
    kind: str | None = None,
    location_id: int | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> InboxPage:
    """One ``UNION ALL`` of both sources, keyset-paged on (created_at, source, id) DESC."""
    require_permission(session, ctx, PROPOSALS_READ)
    after = _decode_cursor(cursor) if cursor else None
    org, now = ctx.organization_id, _now()
    perms = _Permissions(session, ctx)
    now_param = literal(now, DateTime(timezone=True))

    branches = []
    if source in (None, AGENT_SOURCE) and (kind is None or kind in KINDS):
        branches.append(
            select(
                literal_column(f"'{AGENT_SOURCE}'::text").label("source"),
                AgentProposal.id.label("id"),
                AgentProposal.created_at.label("created_at"),
                case(
                    (
                        and_(AgentProposal.status == "pending", AgentProposal.expires_at <= now_param),
                        "expired",
                    ),
                    else_=AgentProposal.status,
                ).label("status"),
                AgentProposal.kind.label("kind"),
                AgentProposal.location_id.label("location_id"),
            ).where(AgentProposal.organization_id == org)
        )
    if (
        source in (None, APPOINTMENT_SOURCE)
        and kind in (None, APPOINTMENT_KIND)
        and perms.has(APPOINTMENTS_READ)
    ):
        branches.append(
            select(
                literal_column(f"'{APPOINTMENT_SOURCE}'::text").label("source"),
                AppointmentProposal.id.label("id"),
                AppointmentProposal.created_at.label("created_at"),
                case(
                    (AppointmentProposal.status == "confirmed", "executed"),
                    (
                        and_(
                            AppointmentProposal.status == "pending",
                            AppointmentProposal.expires_at <= now_param,
                        ),
                        "expired",
                    ),
                    else_=AppointmentProposal.status,
                ).label("status"),
                literal_column(f"'{APPOINTMENT_KIND}'::text").label("kind"),
                AppointmentProposal.location_id.label("location_id"),
            ).where(AppointmentProposal.organization_id == org)
        )
    if not branches:
        return InboxPage(items=[], next_cursor=None)

    inbox = (union_all(*branches) if len(branches) > 1 else branches[0]).subquery("inbox")
    statement = select(inbox.c.source, inbox.c.id, inbox.c.created_at).where(
        inbox.c.status == status
    )
    if kind is not None:
        statement = statement.where(inbox.c.kind == kind)
    if location_id is not None:
        statement = statement.where(inbox.c.location_id == location_id)
    if after is not None:
        statement = statement.where(
            tuple_(inbox.c.created_at, inbox.c.source, inbox.c.id)
            < tuple_(
                literal(after[0], DateTime(timezone=True)),
                literal(after[1], Text),
                literal(after[2], Integer),
            )
        )
    page = session.execute(
        statement.order_by(
            inbox.c.created_at.desc(), inbox.c.source.desc(), inbox.c.id.desc()
        ).limit(limit + 1)
    ).all()
    has_more = len(page) > limit
    page = page[:limit]

    agent_ids = [row.id for row in page if row.source == AGENT_SOURCE]
    appointment_ids = [row.id for row in page if row.source == APPOINTMENT_SOURCE]
    agent = (
        _agent_items(session, ctx, _load_agent_rows(session, org, agent_ids), perms, now)
        if agent_ids
        else {}
    )
    appointments = {}
    if appointment_ids:
        rows = session.execute(
            select(AppointmentProposal, Location.timezone)
            .outerjoin(
                Location,
                and_(
                    Location.organization_id == AppointmentProposal.organization_id,
                    Location.id == AppointmentProposal.location_id,
                ),
            )
            .where(
                AppointmentProposal.organization_id == org,
                AppointmentProposal.id.in_(appointment_ids),
            )
        ).all()
        appointments = _appointment_items(ctx, rows, perms, now)
    items = [
        agent[row.id] if row.source == AGENT_SOURCE else appointments[row.id] for row in page
    ]
    next_cursor = (
        _encode_cursor(page[-1].created_at, page[-1].source, page[-1].id) if has_more else None
    )
    return InboxPage(items=items, next_cursor=next_cursor)
