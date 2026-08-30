"""Read-side slot query and minimal scheduling persistence helpers.

``find_available_slots`` is the HTTP-facing read path: it reuses the
organization eligibility contract and the Task 6 availability engine (never
duplicating the interval algorithm), and returns the strictly chronological
list of bookable intervals per eligible practitioner.

``create_availability_rule`` / ``create_schedule_block`` are deliberately
thin persistence helpers that validate referenced entities and intervals
before delegating to the Task 2 ORM models.

Two scopes coexist here and must be kept apart (PF0 §9 S4): availability rules,
schedule blocks and eligibility are tenant- and location-scoped, while the
conflicting-appointment read stays **practitioner-global** so it mirrors the
GiST exclusion exactly.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.errors import AppError, ErrorCode
from app.organization.models import Location, Practitioner
from app.organization.service import list_eligible_practitioners, load_membership
from app.scheduling.availability import generate_slots
from app.scheduling.models import (
    Appointment,
    AvailabilityRule,
    ScheduleBlock,
)
from app.context import default_context
from app.iam.context import ExecutionContext
from app.iam.permissions import AVAILABILITY_MANAGE, AVAILABILITY_READ
from app.iam.service import require_permission
from app.scheduling.schemas import (
    AvailabilityRuleCreate,
    ScheduleBlockCreate,
)
from app.tenancy import resolve_organization_id, scoped


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    return ctx if ctx is not None else default_context(organization_id)

CONFIRMED = "confirmed"
WEEKDAYS_ES = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)


def _load_active_scoped(
    session: Session, model, entity_id: int, organization_id: int, label: str
):
    entity = session.scalar(
        scoped(select(model).where(model.id == entity_id), model, organization_id)
    )
    if entity is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found.")
    if not entity.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, f"{label} is inactive.")
    return entity


def _load_active_member(
    session: Session, practitioner_id: int, organization_id: int
) -> Practitioner:
    """Load a practitioner the acting organization may actually schedule (PM3)."""
    membership = load_membership(session, practitioner_id, organization_id)
    practitioner = session.get(Practitioner, practitioner_id)
    if practitioner is None:
        raise AppError(ErrorCode.NOT_FOUND, "Practitioner not found.")
    if not practitioner.is_active or not membership.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Practitioner is inactive.")
    return practitioner


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None


def create_availability_rule(
    session: Session,
    data: AvailabilityRuleCreate,
    organization_id: int | None = None,
    ctx: ExecutionContext | None = None,
) -> AvailabilityRule:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(
            session, resolved, AVAILABILITY_MANAGE, location_id=data.location_id
        )
    _load_active_member(session, data.practitioner_id, org_id)
    _load_active_scoped(session, Location, data.location_id, org_id, "Location")
    if data.end_local <= data.start_local:
        raise AppError(
            ErrorCode.INVALID_INPUT, "end_local must be after start_local."
        )
    rule = AvailabilityRule(
        organization_id=org_id,
        practitioner_id=data.practitioner_id,
        location_id=data.location_id,
        day_of_week=data.day_of_week,
        start_local=data.start_local,
        end_local=data.end_local,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def create_schedule_block(
    session: Session,
    data: ScheduleBlockCreate,
    organization_id: int | None = None,
    ctx: ExecutionContext | None = None,
) -> ScheduleBlock:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(
            session, resolved, AVAILABILITY_MANAGE, location_id=data.location_id
        )
    _load_active_member(session, data.practitioner_id, org_id)
    _load_active_scoped(session, Location, data.location_id, org_id, "Location")
    if not _is_aware(data.start_utc) or not _is_aware(data.end_utc):
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "start_utc and end_utc must be timezone-aware.",
        )
    if data.end_utc <= data.start_utc:
        raise AppError(
            ErrorCode.INVALID_INPUT, "end_utc must be after start_utc."
        )
    block = ScheduleBlock(
        organization_id=org_id,
        practitioner_id=data.practitioner_id,
        location_id=data.location_id,
        start_utc=data.start_utc,
        end_utc=data.end_utc,
    )
    session.add(block)
    session.commit()
    session.refresh(block)
    return block


def find_available_slots(
    session: Session,
    service_id: int,
    location_id: int,
    window_start: datetime,
    window_end: datetime,
    organization_id: int | None = None,
    ctx: ExecutionContext | None = None,
) -> list[dict]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(
            session, resolved, AVAILABILITY_READ, location_id=location_id
        )
    service = _load_active_scoped(session, Service, service_id, org_id, "Service")
    location = _load_active_scoped(session, Location, location_id, org_id, "Location")

    if not _is_aware(window_start) or not _is_aware(window_end):
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "window_start and window_end must be timezone-aware.",
        )
    if window_end <= window_start:
        raise AppError(
            ErrorCode.INVALID_INPUT, "window_end must be after window_start."
        )

    practitioners = list_eligible_practitioners(
        session, service_id, location_id, org_id
    )

    results: list[dict] = []
    local_timezone = ZoneInfo(location.timezone)
    for practitioner in practitioners:
        rules = list(
            session.scalars(
                select(AvailabilityRule).where(
                    AvailabilityRule.organization_id == org_id,
                    AvailabilityRule.practitioner_id == practitioner.id,
                    AvailabilityRule.location_id == location_id,
                )
            )
        )
        blocks = list(
            session.scalars(
                select(ScheduleBlock).where(
                    ScheduleBlock.organization_id == org_id,
                    ScheduleBlock.practitioner_id == practitioner.id,
                    ScheduleBlock.location_id == location_id,
                    ScheduleBlock.start_utc < window_end,
                    ScheduleBlock.end_utc > window_start,
                )
            )
        )
        # Practitioner-wide, and deliberately NOT organization-filtered: the
        # GiST exclusion ignores both location and tenant, so a cross-location
        # *or* cross-organization double booking must be surfaced here too
        # (PF0 §9 S4). Adding an organization filter would offer a slot the
        # database then rejects with a raw 23P01.
        appointments = list(
            session.scalars(
                select(Appointment).where(
                    Appointment.practitioner_id == practitioner.id,
                    Appointment.state == CONFIRMED,
                    Appointment.start_utc < window_end,
                    Appointment.end_utc > window_start,
                )
            )
        )
        slots = generate_slots(
            rules,
            blocks,
            appointments,
            service.duration_minutes,
            window_start,
            window_end,
            location.timezone,
        )
        for start_utc, end_utc in slots:
            start_local = start_utc.astimezone(local_timezone)
            end_local = end_utc.astimezone(local_timezone)
            results.append(
                {
                    "practitioner_id": practitioner.id,
                    "start": start_utc,
                    "end": end_utc,
                    "timezone": location.timezone,
                    "start_local": start_local.isoformat(),
                    "end_local": end_local.isoformat(),
                    "date_local": start_local.date().isoformat(),
                    "weekday_local": WEEKDAYS_ES[start_local.weekday()],
                    "time_local": start_local.strftime("%H:%M"),
                }
            )

    results.sort(key=lambda item: (item["start"], item["end"], item["practitioner_id"]))
    return results
