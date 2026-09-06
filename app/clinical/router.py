"""Clinical core HTTP surface: Patient, Visit, ServiceExecution.

Thin transport: HTTP shape → schema → existing application service → typed
response. The `Idempotency-Key` header is passed straight through to the PF4
command handler for every clinical create (C10).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.clinical.schemas import (
    PatientCreate,
    PatientRead,
    ServiceExecutionCreate,
    ServiceExecutionRead,
    VisitCreate,
    VisitDetailRead,
    VisitRead,
)
from app.clinical.service import (
    OP_EXECUTIONS_CREATE,
    OP_PATIENTS_CREATE,
    OP_VISITS_CREATE,
    create_patient,
    create_service_execution,
    create_visit,
    get_patient,
    get_visit,
    list_patients,
    list_executions,
    list_visit_executions,
    list_visits,
)
from app.context import resolve_http_context
from app.db import get_db
from app.idempotency.service import run_idempotent_command

router = APIRouter()

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get(IDEMPOTENCY_HEADER)
    if value is None or value == "":
        return None
    return value


def _patient_read_from_outcome(outcome: dict) -> PatientRead:
    """I5: render the stored logical outcome into the original response schema."""
    return PatientRead(
        id=int(outcome["resource_id"]),
        full_name=outcome["full_name"],
        dni=outcome.get("dni"),
        sexo=outcome.get("sexo"),
        phone=outcome.get("phone"),
        birth_date=outcome.get("birth_date"),
    )


def _patient_read(patient) -> PatientRead:
    return PatientRead(
        id=patient.id,
        full_name=patient.full_name,
        dni=patient.dni,
        sexo=patient.sexo,
        phone=patient.phone,
        birth_date=patient.birth_date,
    )


def _visit_read_from_outcome(outcome: dict) -> VisitRead:
    from datetime import datetime as _dt

    return VisitRead(
        id=int(outcome["resource_id"]),
        patient_id=outcome["patient_id"],
        patient_name=outcome["patient_name"],
        appointment_id=outcome.get("appointment_id"),
        practitioner_id=outcome["practitioner_id"],
        practitioner_name=outcome["practitioner_name"],
        location_id=outcome["location_id"],
        location_name=outcome["location_name"],
        started_at=_dt.fromisoformat(outcome["started_at"]),
    )


def _visit_read(visit) -> VisitRead:
    return VisitRead(
        id=visit.id,
        patient_id=visit.patient_id,
        patient_name=visit.patient.full_name,
        appointment_id=visit.appointment_id,
        practitioner_id=visit.practitioner_id,
        practitioner_name=visit.practitioner.display_name,
        location_id=visit.location_id,
        location_name=visit.location.name,
        started_at=visit.started_at,
    )


def _execution_read_from_outcome(outcome: dict) -> ServiceExecutionRead:
    from datetime import datetime as _dt

    return ServiceExecutionRead(
        id=int(outcome["resource_id"]),
        visit_id=outcome["visit_id"],
        service_id=outcome["service_id"],
        service_name=outcome["service_name"],
        executed_price=outcome["executed_price"],
        executed_at=_dt.fromisoformat(outcome["executed_at"]),
        charge_id=outcome.get("charge_id"),
        patient_id=outcome["patient_id"],
        patient_name=outcome["patient_name"],
        location_id=outcome["location_id"],
    )


def _execution_read(execution) -> ServiceExecutionRead:
    visit = getattr(execution, "_fe3a_visit", None) or execution.visit
    service_name = getattr(execution, "_fe3a_service_name", None)
    if service_name is None:
        service_name = execution.service.name
    patient_id = getattr(execution, "_fe3a_patient_id", None)
    if patient_id is None:
        patient_id = visit.patient_id
    patient_name = getattr(execution, "_fe3a_patient_name", None)
    if patient_name is None:
        patient_name = visit.patient.full_name
    location_id = getattr(execution, "_fe3a_location_id", None)
    if location_id is None:
        location_id = visit.location_id
    return ServiceExecutionRead(
        id=execution.id,
        visit_id=execution.visit_id,
        service_id=execution.service_id,
        service_name=service_name,
        executed_price=execution.executed_price,
        executed_at=execution.executed_at,
        charge_id=getattr(execution, "_fe3a_charge_id", None),
        patient_id=patient_id,
        patient_name=patient_name,
        location_id=location_id,
    )


def _visit_detail_read(visit) -> VisitDetailRead:
    detail = VisitDetailRead(**_visit_read(visit).model_dump(), executions=[])
    executions = getattr(visit, "_fe3a_executions", None)
    if executions is None:
        executions = visit.executions
    detail.executions = [_execution_read(e) for e in executions]
    return detail


@router.post("/patients", response_model=PatientRead, status_code=201)
def create_patient_route(
    payload: PatientCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> PatientRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_patient,
        operation_name=OP_PATIENTS_CREATE,
        key=key,
        ctx=ctx,
        params=payload.model_dump(),
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _patient_read_from_outcome(outcome.outcome)
    return _patient_read(outcome.result)


@router.get("/patients", response_model=list[PatientRead])
def list_patients_route(
    request: Request,
    db: Session = Depends(get_db),
    search: str | None = None,
) -> list[PatientRead]:
    ctx = resolve_http_context(request)
    return [_patient_read(p) for p in list_patients(db, ctx=ctx, search=search)]


@router.get("/patients/{patient_id}", response_model=PatientRead)
def get_patient_route(
    patient_id: int, request: Request, db: Session = Depends(get_db)
) -> PatientRead:
    ctx = resolve_http_context(request)
    return _patient_read(get_patient(db, patient_id, ctx=ctx))


@router.post("/visits", response_model=VisitRead, status_code=201)
def create_visit_route(
    payload: VisitCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> VisitRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_visit,
        operation_name=OP_VISITS_CREATE,
        key=key,
        ctx=ctx,
        params=payload.model_dump(),
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _visit_read_from_outcome(outcome.outcome)
    return _visit_read(outcome.result)


@router.get("/visits", response_model=list[VisitRead])
def list_visits_route(
    request: Request,
    db: Session = Depends(get_db),
    patient_id: int | None = None,
) -> list[VisitRead]:
    ctx = resolve_http_context(request)
    return [_visit_read(v) for v in list_visits(db, ctx=ctx, patient_id=patient_id)]


@router.get("/visits/{visit_id}", response_model=VisitDetailRead)
def get_visit_route(
    visit_id: int, request: Request, db: Session = Depends(get_db)
) -> VisitDetailRead:
    ctx = resolve_http_context(request)
    return _visit_detail_read(get_visit(db, visit_id, ctx=ctx))


@router.get("/visits/{visit_id}/executions", response_model=list[ServiceExecutionRead])
def list_visit_executions_route(
    visit_id: int, request: Request, db: Session = Depends(get_db)
) -> list[ServiceExecutionRead]:
    ctx = resolve_http_context(request)
    return [
        _execution_read(e)
        for e in list_visit_executions(db, visit_id, ctx=ctx)
    ]


@router.get("/executions", response_model=list[ServiceExecutionRead])
def list_executions_route(
    request: Request,
    db: Session = Depends(get_db),
    visit_id: int | None = None,
    patient_id: int | None = None,
    charged: bool | None = None,
    executed_from: date | None = Query(
        default=None, description="Inclusive lower bound on ServiceExecution.executed_at."
    ),
    executed_to: date | None = Query(
        default=None, description="Exclusive upper bound on ServiceExecution.executed_at."
    ),
) -> list[ServiceExecutionRead]:
    ctx = resolve_http_context(request)
    return [
        _execution_read(e)
        for e in list_executions(
            db,
            ctx=ctx,
            visit_id=visit_id,
            patient_id=patient_id,
            charged=charged,
            executed_from=executed_from,
            executed_to=executed_to,
        )
    ]


@router.post("/visits/{visit_id}/executions", response_model=ServiceExecutionRead, status_code=201)
def create_service_execution_route(
    visit_id: int,
    payload: ServiceExecutionCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ServiceExecutionRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_service_execution,
        operation_name=OP_EXECUTIONS_CREATE,
        key=key,
        ctx=ctx,
        params={"visit_id": visit_id, **payload.model_dump()},
        visit_id=visit_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _execution_read_from_outcome(outcome.outcome)
    return _execution_read(outcome.result)
