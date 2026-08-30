"""Scheduling HTTP surface: availability rules, schedule blocks, slot query,
and the appointment booking endpoint with its 40P01 (deadlock) retry policy.

The router stays thin: HTTP shape -> Pydantic schema -> existing application
service / query helper -> typed response. Booking performs no preliminary DB
queries here: ``book_appointment`` owns its transaction and must receive an
idle session.

PF4 adds one transport job only (C10): the optional ``Idempotency-Key`` header
is read and passed straight through to the application command handler
(``app.idempotency.service.run_idempotent_command``), which owns the receipt
claim, the replay and the ``IDEMPOTENCY_KEY_REUSED`` rejection. On a replay
the transport adds the optional, non-authoritative ``Idempotent-Replay: true``
response header (I9) and renders the stored logical outcome (I5) into the same
response schema the original call produced.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.idempotency.service import (
    OP_APPOINTMENTS_BOOK,
    OP_APPOINTMENTS_CANCEL,
    OP_APPOINTMENTS_RESCHEDULE,
    run_idempotent_command,
)
from app.scheduling.query import (
    create_availability_rule,
    create_schedule_block,
    find_available_slots,
)
from app.scheduling.schemas import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentListItem,
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
    get_appointment,
    list_appointments,
    reschedule_appointment,
)

router = APIRouter()

DEADLOCK_SQLSTATE = "40P01"
RETRY_SAFE_MESSAGE = "The requested appointment slot is no longer available."

#: The optional header the transport reads (PF4 C10/I9). Not a schema field:
#: callers never send it in the body.
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get(IDEMPOTENCY_HEADER)
    if value is None or value == "":
        return None
    return value


def _appointment_read_from_outcome(outcome: dict) -> AppointmentRead:
    """I5: render the stored logical outcome into the original response schema.

    The transport owns the rendering; the domain owns the stored outcome.
    """
    return AppointmentRead(
        id=int(outcome["resource_id"]),
        lead_id=outcome["lead_id"],
        service_id=outcome["service_id"],
        practitioner_id=outcome["practitioner_id"],
        location_id=outcome["location_id"],
        start_utc=datetime.fromisoformat(outcome["start_utc"]),
        end_utc=datetime.fromisoformat(outcome["end_utc"]),
        state=outcome["state"],
    )


def _appointment_list_item(appointment) -> AppointmentListItem:
    """Agenda read DTO: the appointment plus its related display names.

    Built inside the route (session still open) so the lazy relationships are
    resolved before the response model renders.
    """
    return AppointmentListItem(
        id=appointment.id,
        lead_id=appointment.lead_id,
        service_id=appointment.service_id,
        practitioner_id=appointment.practitioner_id,
        location_id=appointment.location_id,
        start_utc=appointment.start_utc,
        end_utc=appointment.end_utc,
        state=appointment.state,
        lead_name=appointment.lead.full_name,
        service_name=appointment.service.name,
        practitioner_name=appointment.practitioner.display_name,
        location_name=appointment.location.name,
    )


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
    payload: AvailabilityRuleCreate, request: Request, db: Session = Depends(get_db)
) -> AvailabilityRuleRead:
    ctx = resolve_http_context(request)
    return create_availability_rule(db, payload, ctx=ctx)


@router.post("/schedule-blocks", response_model=ScheduleBlockRead, status_code=201)
def create_schedule_block_route(
    payload: ScheduleBlockCreate, request: Request, db: Session = Depends(get_db)
) -> ScheduleBlockRead:
    ctx = resolve_http_context(request)
    return create_schedule_block(db, payload, ctx=ctx)


@router.post("/slots/query", response_model=list[SlotResult])
def query_slots_route(
    payload: SlotQuery, request: Request, db: Session = Depends(get_db)
) -> list[SlotResult]:
    # Availability is tenant data: without the context this answered for the
    # bootstrap organization regardless of who asked.
    ctx = resolve_http_context(request)
    return find_available_slots(
        db,
        payload.service_id,
        payload.location_id,
        payload.window_start,
        payload.window_end,
        ctx=ctx,
    )


@router.get("/appointments", response_model=list[AppointmentListItem])
def list_appointments_route(
    request: Request,
    db: Session = Depends(get_db),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    location_id: int | None = None,
    practitioner_id: int | None = None,
) -> list[AppointmentListItem]:
    """Agenda read: the acting organization's appointments, half-open window.

    The window is ``[from_date, to_date)``; both bounds are optional and
    passed as RFC 3339 instants. Location/practitioner filters narrow the
    result; the tenant always comes from the context, never from a query
    parameter (X3).
    """
    ctx = resolve_http_context(request)
    appointments = list_appointments(
        db,
        ctx=ctx,
        from_date=from_date,
        to_date=to_date,
        location_id=location_id,
        practitioner_id=practitioner_id,
    )
    return [_appointment_list_item(a) for a in appointments]


@router.get("/appointments/{appointment_id}", response_model=AppointmentListItem)
def get_appointment_route(
    appointment_id: int, request: Request, db: Session = Depends(get_db)
) -> AppointmentListItem:
    ctx = resolve_http_context(request)
    return _appointment_list_item(get_appointment(db, appointment_id, ctx=ctx))


@router.post("/appointments", response_model=AppointmentRead, status_code=201)
def create_appointment_route(
    payload: AppointmentCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    operation: Callable = Depends(get_booking_operation),
) -> AppointmentRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    params = payload.model_dump()

    def _command(session: Session, **kwargs):
        return run_idempotent_command(
            session,
            operation=operation,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=key,
            ctx=ctx,
            params=params,
            **kwargs,
        )

    # The 40P01 retry wraps the whole idempotent command (C8): a deadlock
    # rolls the claim back with the attempt (C3) and the retry re-claims.
    outcome = book_appointment_with_retry(db, operation=_command, **params)
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _appointment_read_from_outcome(outcome.outcome)
    return outcome.result


# Cancellation and rescheduling deliberately skip the booking retry policy:
# both take the appointment row ``FOR UPDATE`` as their first statement, so
# competing mutations of the same appointment queue on that lock instead of
# racing into a deadlock. A conflicting *new* interval is still settled by the
# GiST exclusion and mapped to 409 by the Task 3 transport handler.


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead, status_code=200)
def cancel_appointment_route(
    appointment_id: int,
    request: Request,
    response: Response,
    payload: AppointmentCancel | None = None,
    db: Session = Depends(get_db),
) -> AppointmentRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    params = {"appointment_id": appointment_id}
    outcome = run_idempotent_command(
        db,
        operation=cancel_appointment,
        operation_name=OP_APPOINTMENTS_CANCEL,
        key=key,
        ctx=ctx,
        params=params,
        appointment_id=appointment_id,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _appointment_read_from_outcome(outcome.outcome)
    return outcome.result


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentRead, status_code=200)
def reschedule_appointment_route(
    appointment_id: int,
    request: Request,
    response: Response,
    payload: AppointmentReschedule,
    db: Session = Depends(get_db),
) -> AppointmentRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    params = {"appointment_id": appointment_id, "new_start": payload.new_start}
    outcome = run_idempotent_command(
        db,
        operation=reschedule_appointment,
        operation_name=OP_APPOINTMENTS_RESCHEDULE,
        key=key,
        ctx=ctx,
        params=params,
        appointment_id=appointment_id,
        new_start=payload.new_start,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _appointment_read_from_outcome(outcome.outcome)
    return outcome.result
