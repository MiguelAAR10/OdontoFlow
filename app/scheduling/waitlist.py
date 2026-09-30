"""Minimal appointment waitlist (B0.5): model, contracts, services and routes.

A waitlist entry records that a lead wants a service within a date window,
optionally at one location / with one practitioner / in a time-of-day window.
B0.5 only creates, lists and cancels entries; ``offered``/``booked``/``expired``
exist in the CHECK for B4, which will drive those transitions.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.audit.service import record_event
from app.catalog.models import Service
from app.clinical.models import Patient
from app.commercial.models import Lead
from app.context import default_context, resolve_http_context
from app.db import Base, get_db
from app.errors import AppError, ErrorCode
from app.events.service import record_domain_event
from app.events.types import WAITLIST_CREATED
from app.iam.context import ExecutionContext
from app.iam.permissions import WAITLIST_MANAGE, WAITLIST_READ
from app.iam.service import require_permission
from app.idempotency.service import (
    IdempotencyClaim,
    claim_receipt,
    run_idempotent_command,
    settle_receipt,
)
from app.organization.models import Location
from app.scheduling.service import _load_active_member, _load_active_scoped
from app.tenancy import scoped

WAITLIST_STATUSES = ("open", "offered", "booked", "cancelled", "expired")
CANCELLABLE_STATUSES = ("open", "offered")
PREFERRED_WINDOWS = ("any", "morning", "afternoon")
WAITLIST_ENTITY_TYPE = "waitlist_entry"
WAITLIST_CREATED_ACTION = "waitlist_entry.created"
WAITLIST_CANCELLED_ACTION = "waitlist_entry.cancelled"
OP_WAITLIST_CREATE = "waitlist.create"
OP_WAITLIST_CANCEL = "waitlist.cancel"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_waitlist_entries_organization"),
        nullable=False,
    )
    lead_id: Mapped[int] = mapped_column(nullable=False)
    patient_id: Mapped[int | None] = mapped_column(nullable=True)
    service_id: Mapped[int] = mapped_column(nullable=False)
    location_id: Mapped[int | None] = mapped_column(nullable=True)
    practitioner_id: Mapped[int | None] = mapped_column(nullable=True)
    earliest_date: Mapped[date] = mapped_column(Date, nullable=False)
    latest_date: Mapped[date] = mapped_column(Date, nullable=False)
    preferred_window: Mapped[str] = mapped_column(String(10), nullable=False, server_default="any")
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("latest_date >= earliest_date", name="ck_waitlist_entries_dates"),
        CheckConstraint(
            "preferred_window IN ('any', 'morning', 'afternoon')",
            name="ck_waitlist_entries_window",
        ),
        CheckConstraint(
            "status IN ('open', 'offered', 'booked', 'cancelled', 'expired')",
            name="ck_waitlist_entries_status",
        ),
        UniqueConstraint("organization_id", "id", name="uq_waitlist_entries_organization_id"),
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            ondelete="RESTRICT",
            name="fk_waitlist_entries_organization_lead",
        ),
        ForeignKeyConstraint(
            ["organization_id", "patient_id"],
            ["patients.organization_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_waitlist_entries_organization_patient",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_waitlist_entries_organization_service",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_waitlist_entries_organization_location",
        ),
        ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_waitlist_entries_organization_membership",
        ),
        Index("ix_waitlist_entries_org_status_service", "organization_id", "status", "service_id"),
    )


# --- contracts ----------------------------------------------------------------


class WaitlistEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int
    patient_id: int | None = None
    service_id: int
    location_id: int | None = None
    practitioner_id: int | None = None
    earliest_date: date
    latest_date: date
    preferred_window: Literal["any", "morning", "afternoon"] = "any"
    notes: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "WaitlistEntryCreate":
        if self.latest_date < self.earliest_date:
            raise ValueError("latest_date must be on or after earliest_date.")
        return self


class WaitlistEntryCancel(BaseModel):
    """Empty by design: the entry is identified by the path."""

    model_config = ConfigDict(extra="forbid")


class WaitlistEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    patient_id: int | None
    service_id: int
    location_id: int | None
    practitioner_id: int | None
    earliest_date: date
    latest_date: date
    preferred_window: str
    status: str
    notes: str | None
    created_at: datetime


# --- services -----------------------------------------------------------------


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    return ctx if ctx is not None else default_context(organization_id)


def _entry_state(entry: WaitlistEntry) -> dict:
    return {
        "id": entry.id,
        "lead_id": entry.lead_id,
        "patient_id": entry.patient_id,
        "service_id": entry.service_id,
        "location_id": entry.location_id,
        "practitioner_id": entry.practitioner_id,
        "earliest_date": entry.earliest_date.isoformat(),
        "latest_date": entry.latest_date.isoformat(),
        "preferred_window": entry.preferred_window,
        "status": entry.status,
    }


def _entry_outcome(entry: WaitlistEntry) -> dict:
    return {
        "status": "applied",
        "resource_type": WAITLIST_ENTITY_TYPE,
        "resource_id": str(entry.id),
        "entry": {
            **_entry_state(entry),
            "notes": entry.notes,
            "created_at": entry.created_at.isoformat(),
        },
    }


def create_waitlist_entry(
    session: Session,
    data: WaitlistEntryCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> WaitlistEntry:
    """Add one ``open`` entry; every reference is resolved inside the tenant."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, WAITLIST_MANAGE)
        lead = session.scalar(scoped(select(Lead).where(Lead.id == data.lead_id), Lead, org_id))
        if lead is None:
            raise AppError(ErrorCode.NOT_FOUND, "Lead not found.")
        if data.patient_id is not None and session.scalar(
            scoped(select(Patient).where(Patient.id == data.patient_id), Patient, org_id)
        ) is None:
            raise AppError(ErrorCode.NOT_FOUND, "Patient not found.")
        _load_active_scoped(session, Service, data.service_id, org_id, "Service")
        if data.location_id is not None:
            _load_active_scoped(session, Location, data.location_id, org_id, "Location")
        if data.practitioner_id is not None:
            _load_active_member(session, data.practitioner_id, org_id)

        entry = WaitlistEntry(organization_id=org_id, status="open", **data.model_dump())
        session.add(entry)
        session.flush()
        session.refresh(entry, ["created_at"])

        record_event(
            session,
            ctx=resolved,
            entity_type=WAITLIST_ENTITY_TYPE,
            entity_id=str(entry.id),
            action=WAITLIST_CREATED_ACTION,
            after_state=_entry_state(entry),
        )
        record_domain_event(
            session,
            ctx=resolved,
            event_type=WAITLIST_CREATED,
            aggregate_type=WAITLIST_ENTITY_TYPE,
            aggregate_id=str(entry.id),
            payload=_entry_state(entry),
        )
        settle_receipt(
            receipt,
            resource_type=WAITLIST_ENTITY_TYPE,
            resource_id=str(entry.id),
            outcome_json=_entry_outcome(entry),
        )

    return entry


def list_waitlist_entries(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    status: str | None = None,
    location_id: int | None = None,
    service_id: int | None = None,
) -> list[WaitlistEntry]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, WAITLIST_READ)
    statement = scoped(select(WaitlistEntry), WaitlistEntry, org_id).order_by(
        WaitlistEntry.created_at, WaitlistEntry.id
    )
    if status is not None:
        statement = statement.where(WaitlistEntry.status == status)
    if location_id is not None:
        statement = statement.where(WaitlistEntry.location_id == location_id)
    if service_id is not None:
        statement = statement.where(WaitlistEntry.service_id == service_id)
    return list(session.scalars(statement))


def cancel_waitlist_entry(
    session: Session,
    entry_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> WaitlistEntry:
    """``open``/``offered`` → ``cancelled``; anything else is ``ENTITY_INACTIVE``."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, WAITLIST_MANAGE)
        entry = session.scalar(
            scoped(select(WaitlistEntry).where(WaitlistEntry.id == entry_id), WaitlistEntry, org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is None:
            raise AppError(ErrorCode.NOT_FOUND, "Waitlist entry not found.")
        if entry.status not in CANCELLABLE_STATUSES:
            raise AppError(ErrorCode.ENTITY_INACTIVE, "The waitlist entry is no longer open.")
        before_state = _entry_state(entry)
        entry.status = "cancelled"
        session.flush()
        record_event(
            session,
            ctx=resolved,
            entity_type=WAITLIST_ENTITY_TYPE,
            entity_id=str(entry.id),
            action=WAITLIST_CANCELLED_ACTION,
            before_state=before_state,
            after_state=_entry_state(entry),
        )
        settle_receipt(
            receipt,
            resource_type=WAITLIST_ENTITY_TYPE,
            resource_id=str(entry.id),
            outcome_json=_entry_outcome(entry),
        )

    return entry


# --- HTTP ---------------------------------------------------------------------

router = APIRouter()


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get(IDEMPOTENCY_HEADER)
    return value or None


def _read_from_outcome(outcome: dict) -> WaitlistEntryRead:
    return WaitlistEntryRead.model_validate(outcome["entry"])


@router.post("/waitlist", response_model=WaitlistEntryRead, status_code=201)
def create_waitlist_entry_route(
    payload: WaitlistEntryCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> WaitlistEntryRead:
    ctx = resolve_http_context(request)
    outcome = run_idempotent_command(
        db,
        operation=create_waitlist_entry,
        operation_name=OP_WAITLIST_CREATE,
        key=_idempotency_key(request),
        ctx=ctx,
        params=payload.model_dump(mode="json"),
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _read_from_outcome(outcome.outcome)
    return WaitlistEntryRead.model_validate(outcome.result)


@router.get("/waitlist", response_model=list[WaitlistEntryRead])
def list_waitlist_entries_route(
    request: Request,
    db: Session = Depends(get_db),
    status: Literal["open", "offered", "booked", "cancelled", "expired"] | None = None,
    location_id: int | None = Query(default=None),
    service_id: int | None = Query(default=None),
) -> list[WaitlistEntryRead]:
    ctx = resolve_http_context(request)
    return [
        WaitlistEntryRead.model_validate(entry)
        for entry in list_waitlist_entries(
            db, ctx=ctx, status=status, location_id=location_id, service_id=service_id
        )
    ]


@router.post("/waitlist/{entry_id}/cancel", response_model=WaitlistEntryRead, status_code=200)
def cancel_waitlist_entry_route(
    entry_id: int,
    request: Request,
    response: Response,
    payload: WaitlistEntryCancel | None = None,
    db: Session = Depends(get_db),
) -> WaitlistEntryRead:
    ctx = resolve_http_context(request)
    outcome = run_idempotent_command(
        db,
        operation=cancel_waitlist_entry,
        operation_name=OP_WAITLIST_CANCEL,
        key=_idempotency_key(request),
        ctx=ctx,
        params={"entry_id": entry_id},
        entry_id=entry_id,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _read_from_outcome(outcome.outcome)
    return WaitlistEntryRead.model_validate(outcome.result)
