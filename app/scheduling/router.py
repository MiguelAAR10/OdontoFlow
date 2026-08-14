"""Scheduling HTTP surface: availability rules, schedule blocks, slot query,
and the appointment booking endpoint with its 40P01 (deadlock) retry policy.

The router stays thin: HTTP shape -> Pydantic schema -> existing application
service / query helper -> typed response. Booking performs no preliminary DB
queries here: ``book_appointment`` owns its transaction and must receive an
idle session.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import AppError, ErrorCode
from app.scheduling.query import (
    create_availability_rule,
    create_schedule_block,
    find_available_slots,
)
from app.scheduling.schemas import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentReschedule,
    AvailabilityRuleCreate,
    AvailabilityRuleRead,
    ScheduleBlockCreate,
    ScheduleBlockRead,
    SlotQuery,
    SlotResult,
)
from app.scheduling.service import (
    book_appointment,
    cancel_appointment,
    reschedule_appointment,
)

router = APIRouter()

DEADLOCK_SQLSTATE = "40P01"
RETRY_SAFE_MESSAGE = "The requested appointment slot is no longer available."


def _sqlstate(exc) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if state is None:
        diag = getattr(orig, "diag", None)
        state = getattr(diag, "sqlstate", None) if diag is not None else None
    return str(state) if state else None


def get_booking_operation() -> Callable:
    return book_appointment


def book_appointment_with_retry(
    session: Session, *, operation: Callable | None = None, **kwargs
):
    """Call the booking operation once, retrying a deadlock exactly once.

    * ``23P01`` (exclusion violation) propagates untouched: the Task 3
      transport handler maps it to ``409 APPOINTMENT_CONFLICT``.
    * ``40P01`` (deadlock) on the first attempt rolls the session back and
      retries the complete operation once. A second deadlock is surfaced as a
      safe booking conflict, never as raw DB internals.
    * Any other error propagates untouched.
    """
    op = operation or book_appointment

    def _attempt():
        return op(session, **kwargs)

    try:
        return _attempt()
    except (OperationalError, IntegrityError) as exc:
        if _sqlstate(exc) != DEADLOCK_SQLSTATE:
            raise
        session.rollback()
        try:
            return _attempt()
        except (OperationalError, IntegrityError) as retry_exc:
            if _sqlstate(retry_exc) != DEADLOCK_SQLSTATE:
                raise
            raise AppError(
                ErrorCode.APPOINTMENT_CONFLICT, RETRY_SAFE_MESSAGE
            ) from retry_exc


@router.post("/availability-rules", response_model=AvailabilityRuleRead, status_code=201)
def create_availability_rule_route(
    payload: AvailabilityRuleCreate, db: Session = Depends(get_db)
) -> AvailabilityRuleRead:
    return create_availability_rule(db, payload)


@router.post("/schedule-blocks", response_model=ScheduleBlockRead, status_code=201)
def create_schedule_block_route(
    payload: ScheduleBlockCreate, db: Session = Depends(get_db)
) -> ScheduleBlockRead:
    return create_schedule_block(db, payload)


@router.post("/slots/query", response_model=list[SlotResult])
def query_slots_route(
    payload: SlotQuery, db: Session = Depends(get_db)
) -> list[SlotResult]:
    return find_available_slots(
        db,
        payload.service_id,
        payload.location_id,
        payload.window_start,
        payload.window_end,
    )


@router.post("/appointments", response_model=AppointmentRead, status_code=201)
def create_appointment_route(
    payload: AppointmentCreate,
    db: Session = Depends(get_db),
    operation: Callable = Depends(get_booking_operation),
) -> AppointmentRead:
    return book_appointment_with_retry(
        db, operation=operation, **payload.model_dump()
    )


# Cancellation and rescheduling deliberately skip the booking retry policy:
# both take the appointment row ``FOR UPDATE`` as their first statement, so
# competing mutations of the same appointment queue on that lock instead of
# racing into a deadlock. A conflicting *new* interval is still settled by the
# GiST exclusion and mapped to 409 by the Task 3 transport handler.


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead, status_code=200)
def cancel_appointment_route(
    appointment_id: int,
    payload: AppointmentCancel | None = None,
    db: Session = Depends(get_db),
) -> AppointmentRead:
    return cancel_appointment(db, appointment_id)


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentRead, status_code=200)
def reschedule_appointment_route(
    appointment_id: int,
    payload: AppointmentReschedule,
    db: Session = Depends(get_db),
) -> AppointmentRead:
    return reschedule_appointment(db, appointment_id, payload.new_start)
