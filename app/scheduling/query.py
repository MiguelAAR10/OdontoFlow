"""Read-side slot query and minimal scheduling persistence helpers.

``find_available_slots`` is the HTTP-facing read path: it reuses the
organization eligibility contract and the Task 6 availability engine (never
duplicating the interval algorithm), and returns the strictly chronological
list of bookable intervals per eligible practitioner.

``create_availability_rule`` / ``create_schedule_block`` are deliberately
thin persistence helpers that validate referenced entities and intervals
before delegating to the Task 2 ORM models.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.errors import AppError, ErrorCode
from app.organization.models import Location, Practitioner
from app.organization.service import list_eligible_practitioners
from app.scheduling.availability import generate_slots
from app.scheduling.models import (
    Appointment,
    AvailabilityRule,
    ScheduleBlock,
)
from app.scheduling.schemas import (
    AvailabilityRuleCreate,
    ScheduleBlockCreate,
)

CONFIRMED = "confirmed"


def _load_active(session: Session, model, entity_id: int, label: str):
    entity = session.get(model, entity_id)
    if entity is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found.")
    if not entity.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, f"{label} is inactive.")
    return entity


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None


def create_availability_rule(
    session: Session, data: AvailabilityRuleCreate
) -> AvailabilityRule:
    _load_active(session, Practitioner, data.practitioner_id, "Practitioner")
    _load_active(session, Location, data.location_id, "Location")
    if data.end_local <= data.start_local:
        raise AppError(
            ErrorCode.INVALID_INPUT, "end_local must be after start_local."
        )
    rule = AvailabilityRule(
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
    session: Session, data: ScheduleBlockCreate
) -> ScheduleBlock:
    _load_active(session, Practitioner, data.practitioner_id, "Practitioner")
    _load_active(session, Location, data.location_id, "Location")
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
) -> list[dict]:
    service = _load_active(session, Service, service_id, "Service")
    location = _load_active(session, Location, location_id, "Location")

    if not _is_aware(window_start) or not _is_aware(window_end):
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "window_start and window_end must be timezone-aware.",
        )
    if window_end <= window_start:
        raise AppError(
            ErrorCode.INVALID_INPUT, "window_end must be after window_start."
        )

    practitioners = list_eligible_practitioners(session, service_id, location_id)

    results: list[dict] = []
    for practitioner in practitioners:
        rules = list(
            session.scalars(
                select(AvailabilityRule).where(
                    AvailabilityRule.practitioner_id == practitioner.id,
                    AvailabilityRule.location_id == location_id,
                )
            )
        )
        blocks = list(
            session.scalars(
                select(ScheduleBlock).where(
                    ScheduleBlock.practitioner_id == practitioner.id,
                    ScheduleBlock.location_id == location_id,
                    ScheduleBlock.start_utc < window_end,
                    ScheduleBlock.end_utc > window_start,
                )
            )
        )
        # Practitioner-wide, not location-scoped: the GiST exclusion ignores
        # the location, so a cross-location double booking must be surfaced
        # here too (mirrors the booking preflight contract).
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
            results.append(
                {
                    "practitioner_id": practitioner.id,
                    "start": start_utc,
                    "end": end_utc,
                }
            )

    results.sort(key=lambda item: (item["start"], item["end"], item["practitioner_id"]))
    return results
