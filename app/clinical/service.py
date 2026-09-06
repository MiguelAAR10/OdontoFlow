"""Clinical core application services (Patient, Visit, ServiceExecution).

Every command follows the established module conventions: the caller hands an
idle ``Session`` and an explicit ``ExecutionContext`` (PF3); the tenant comes
from the context, never from the body (X3); ``require_permission`` is the
first statement after the idempotency claim (E6); the audit row is staged in
the same transaction (PF3); idempotent commands claim first and settle before
commit (PF4 §16.1).

Legacy semantics preserved (see ``.audit/accelerator/clinical-legacy.md``):
DNI as the durable clinic identity (now per-organization); visit header with
multiple executed service lines; a service executes at most once per visit;
the executed price is a point-in-time snapshot owned by the execution row.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.catalog.models import Service
from app.clinical.models import Patient, ServiceExecution, Visit
from app.clinical.schemas import PatientCreate, ServiceExecutionCreate, VisitCreate
from app.context import default_context
from app.errors import AppError, ErrorCode
from app.economics.models import Charge
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    EXECUTIONS_CREATE,
    EXECUTIONS_READ,
    PATIENTS_CREATE,
    PATIENTS_READ,
    VISITS_CREATE,
    VISITS_READ,
)
from app.iam.service import require_permission
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.scheduling.models import Appointment
from app.tenancy import scoped

#: The PF4 operation strings for the clinical commands.
OP_PATIENTS_CREATE = "patients.create"
OP_VISITS_CREATE = "visits.create"
OP_EXECUTIONS_CREATE = "executions.create"

PATIENT_ENTITY_TYPE = "patient"
VISIT_ENTITY_TYPE = "visit"
EXECUTION_ENTITY_TYPE = "service_execution"
PATIENT_CREATED_ACTION = "patient.created"
VISIT_CREATED_ACTION = "visit.created"
EXECUTION_CREATED_ACTION = "service_execution.created"


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    """Explicit ctx wins; otherwise the trusted/default context (PF3 seam)."""
    return ctx if ctx is not None else default_context(organization_id)


def _load_active_scoped(
    session: Session, model, entity_id: int, organization_id: int, label: str
):
    entity = session.scalar(
        scoped(select(model).where(model.id == entity_id), model, organization_id)
    )
    if entity is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found.")
    if getattr(entity, "is_active", None) is False:
        raise AppError(ErrorCode.ENTITY_INACTIVE, f"{label} is inactive.")
    return entity


def _patient_outcome(patient: Patient) -> dict:
    return {
        "status": "applied",
        "resource_type": PATIENT_ENTITY_TYPE,
        "resource_id": str(patient.id),
        "full_name": patient.full_name,
        "dni": patient.dni,
        "sexo": patient.sexo,
        "phone": patient.phone,
        "birth_date": patient.birth_date.isoformat() if patient.birth_date else None,
    }


def _visit_outcome(visit: Visit) -> dict:
    return {
        "status": "applied",
        "resource_type": VISIT_ENTITY_TYPE,
        "resource_id": str(visit.id),
        "patient_id": visit.patient_id,
        "patient_name": visit.patient.full_name,
        "appointment_id": visit.appointment_id,
        "practitioner_id": visit.practitioner_id,
        "practitioner_name": visit.practitioner.display_name,
        "location_id": visit.location_id,
        "location_name": visit.location.name,
        "started_at": visit.started_at.isoformat(),
    }


def _execution_outcome(execution: ServiceExecution) -> dict:
    visit = execution.visit
    return {
        "status": "applied",
        "resource_type": EXECUTION_ENTITY_TYPE,
        "resource_id": str(execution.id),
        "visit_id": execution.visit_id,
        "service_id": execution.service_id,
        "service_name": execution.service.name,
        "executed_price": str(execution.executed_price),
        "executed_at": execution.executed_at.isoformat(),
        "charge_id": None,
        "patient_id": visit.patient_id,
        "patient_name": visit.patient.full_name,
        "location_id": visit.location_id,
    }


DUPLICATE_EXECUTION_CONSTRAINT = "uq_service_executions_org_visit_service"
DUPLICATE_VISIT_APPOINTMENT_INDEX = "uq_visits_org_appointment"


def _is_duplicate_visit_appointment(exc: IntegrityError) -> bool:
    """C7-style discrimination for the FE3A attendance guard."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is None:
        diag = getattr(orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
    if str(sqlstate) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    return diag is not None and getattr(diag, "constraint_name", None) == DUPLICATE_VISIT_APPOINTMENT_INDEX


def _is_duplicate_execution(exc: IntegrityError) -> bool:
    """C7-style discrimination: this 23505 is the per-visit service duplicate."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is None:
        diag = getattr(orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
    if str(sqlstate) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    if diag is None:
        return False
    return getattr(diag, "constraint_name", None) == DUPLICATE_EXECUTION_CONSTRAINT


# --- Patient ----------------------------------------------------------------


def create_patient(
    session: Session,
    data: PatientCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Patient:
    """Register one organization-owned patient.

    The DNI uniqueness rule is pre-validated for a stable error and backed by
    the partial unique index ``uq_patients_org_dni`` (the database stays the
    final authority for races).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, PATIENTS_CREATE)

        if data.dni is not None:
            existing = session.scalar(
                scoped(
                    select(Patient).where(Patient.dni == data.dni),
                    Patient,
                    org_id,
                )
            )
            if existing is not None:
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    f"Ya existe un paciente con ese DNI: {data.dni}",
                )

        patient = Patient(
            organization_id=org_id,
            full_name=data.full_name,
            dni=data.dni,
            sexo=data.sexo,
            phone=data.phone,
            birth_date=data.birth_date,
        )
        session.add(patient)
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=PATIENT_ENTITY_TYPE,
            entity_id=str(patient.id),
            action=PATIENT_CREATED_ACTION,
            after_state={"id": patient.id, "full_name": patient.full_name, "dni": patient.dni},
        )
        settle_receipt(
            receipt,
            resource_type=PATIENT_ENTITY_TYPE,
            resource_id=str(patient.id),
            outcome_json=_patient_outcome(patient),
        )

    return patient


def list_patients(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    search: str | None = None,
) -> list[Patient]:
    """Tenant-scoped patient list with name/DNI substring search."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PATIENTS_READ)

    statement = scoped(select(Patient), Patient, org_id).order_by(Patient.full_name)
    if search:
        like = f"%{search}%"
        statement = statement.where(
            or_(Patient.full_name.ilike(like), Patient.dni.ilike(like))
        )
    return list(session.scalars(statement))


def get_patient(
    session: Session,
    patient_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> Patient:
    """One patient, tenant-scoped (another org's id is NOT_FOUND, E8)."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PATIENTS_READ)
    patient = session.scalar(
        scoped(select(Patient).where(Patient.id == patient_id), Patient, org_id)
    )
    if patient is None:
        raise AppError(ErrorCode.NOT_FOUND, "Patient not found.")
    return patient


# --- Visit ------------------------------------------------------------------


def create_visit(
    session: Session,
    data: VisitCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Visit:
    """Record one attended clinical encounter.

    Appointment-origin rule (proven domain rule): an appointment, when given,
    must belong to the organization and be ``confirmed`` — a reservation that
    was cancelled or never confirmed cannot originate attendance; the
    practitioner and location are then derived from the appointment. Without
    an appointment (walk-in), both are required explicitly. The attendance
    instant is domain-owned (server default).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, VISITS_CREATE)

        _load_active_scoped(session, Patient, data.patient_id, org_id, "Patient")

        if data.appointment_id is not None:
            appointment = session.scalar(
                scoped(
                    select(Appointment).where(Appointment.id == data.appointment_id),
                    Appointment,
                    org_id,
                )
            )
            if appointment is None:
                raise AppError(ErrorCode.NOT_FOUND, "Appointment not found.")
            if appointment.state != "confirmed":
                raise AppError(
                    ErrorCode.ENTITY_INACTIVE,
                    "Only a confirmed appointment can originate a visit.",
                )
            practitioner_id = appointment.practitioner_id
            location_id = appointment.location_id
        else:
            if data.practitioner_id is None or data.location_id is None:
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "practitioner_id and location_id are required when no appointment is given.",
                )
            practitioner_id = data.practitioner_id
            location_id = data.location_id

        _load_active_member(session, practitioner_id, org_id)
        _load_active_scoped(session, Location, location_id, org_id, "Location")

        visit = Visit(
            organization_id=org_id,
            patient_id=data.patient_id,
            appointment_id=data.appointment_id,
            practitioner_id=practitioner_id,
            location_id=location_id,
        )
        session.add(visit)
        try:
            session.flush()
        except IntegrityError as exc:
            # The partial unique index is the final authority under two
            # concurrent operators.  Keep the public error stable and never
            # leak the PostgreSQL index name through the HTTP envelope.
            if _is_duplicate_visit_appointment(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The appointment already has a visit.",
                ) from exc
            raise

        record_event(
            session,
            ctx=resolved,
            entity_type=VISIT_ENTITY_TYPE,
            entity_id=str(visit.id),
            action=VISIT_CREATED_ACTION,
            after_state={
                "id": visit.id,
                "patient_id": visit.patient_id,
                "appointment_id": visit.appointment_id,
                "practitioner_id": visit.practitioner_id,
                "location_id": visit.location_id,
            },
        )
        settle_receipt(
            receipt,
            resource_type=VISIT_ENTITY_TYPE,
            resource_id=str(visit.id),
            outcome_json=_visit_outcome(visit),
        )

    return visit


def list_visits(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    patient_id: int | None = None,
) -> list[Visit]:
    """Tenant-scoped visit list, optionally filtered by patient."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, VISITS_READ)

    statement = scoped(select(Visit), Visit, org_id).order_by(Visit.started_at.desc())
    if patient_id is not None:
        statement = statement.where(Visit.patient_id == patient_id)
    return list(session.scalars(statement))


def get_visit(
    session: Session,
    visit_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> Visit:
    """One visit with its executions, tenant-scoped (E8)."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, VISITS_READ)
    visit = session.scalar(
        scoped(select(Visit).where(Visit.id == visit_id), Visit, org_id)
    )
    if visit is None:
        raise AppError(ErrorCode.NOT_FOUND, "Visit not found.")
    # Keep the shared ServiceExecutionRead contract truthful on the visit
    # detail path as well as on GET /executions: a charge already attached to
    # an execution must not be rendered as ``charge_id=null`` merely because
    # the detail endpoint reached the relationship through Visit.
    visit._fe3a_executions = _execution_projection_rows(
        session, org_id, visit_id=visit.id
    )
    return visit


def list_visit_executions(
    session: Session,
    visit_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> list[ServiceExecution]:
    """The executed services of one visit, tenant-scoped.

    The visit existence check is the tenant filter (another org's visit is
    NOT_FOUND, E8); the permission gate is ``executions.read``.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, EXECUTIONS_READ)
    visit = session.scalar(
        scoped(select(Visit).where(Visit.id == visit_id), Visit, org_id)
    )
    if visit is None:
        raise AppError(ErrorCode.NOT_FOUND, "Visit not found.")
    return _execution_projection_rows(session, org_id, visit_id=visit.id)


def _utc_date_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _execution_projection_rows(
    session: Session,
    organization_id: int,
    *,
    visit_id: int | None = None,
    patient_id: int | None = None,
    charged: bool | None = None,
    executed_from: date | None = None,
    executed_to: date | None = None,
) -> list[ServiceExecution]:
    """Load execution rows and all BE-2 context in one bounded query."""
    statement = (
        select(ServiceExecution, Visit, Patient, Service, Charge)
        .join(
            Visit,
            and_(
                Visit.organization_id == ServiceExecution.organization_id,
                Visit.id == ServiceExecution.visit_id,
            ),
        )
        .join(
            Patient,
            and_(
                Patient.organization_id == Visit.organization_id,
                Patient.id == Visit.patient_id,
            ),
        )
        .join(
            Service,
            and_(
                Service.organization_id == ServiceExecution.organization_id,
                Service.id == ServiceExecution.service_id,
            ),
        )
        .outerjoin(
            Charge,
            and_(
                Charge.organization_id == ServiceExecution.organization_id,
                Charge.service_execution_id == ServiceExecution.id,
            ),
        )
        .where(ServiceExecution.organization_id == organization_id)
        .order_by(ServiceExecution.executed_at.desc(), ServiceExecution.id.desc())
    )
    if visit_id is not None:
        statement = statement.where(ServiceExecution.visit_id == visit_id)
    if patient_id is not None:
        statement = statement.where(Visit.patient_id == patient_id)
    if charged is True:
        statement = statement.where(Charge.id.is_not(None))
    elif charged is False:
        statement = statement.where(Charge.id.is_(None))
    if executed_from is not None:
        statement = statement.where(ServiceExecution.executed_at >= _utc_date_start(executed_from))
    if executed_to is not None:
        statement = statement.where(ServiceExecution.executed_at < _utc_date_start(executed_to))

    executions: list[ServiceExecution] = []
    for execution, visit, patient, service, charge in session.execute(statement).all():
        # The ORM rows remain the service return type for compatibility.  The
        # projection values are transient, read-only attributes consumed by the
        # HTTP mapper and never persisted.
        execution._fe3a_visit = visit
        execution._fe3a_patient_id = patient.id
        execution._fe3a_patient_name = patient.full_name
        execution._fe3a_location_id = visit.location_id
        execution._fe3a_charge_id = charge.id if charge is not None else None
        execution._fe3a_service_name = service.name
        executions.append(execution)
    return executions


def list_executions(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    visit_id: int | None = None,
    patient_id: int | None = None,
    charged: bool | None = None,
    executed_from: date | None = None,
    executed_to: date | None = None,
) -> list[ServiceExecution]:
    """Operational queue of executed services, including uncharged work."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, EXECUTIONS_READ)
    return _execution_projection_rows(
        session,
        org_id,
        visit_id=visit_id,
        patient_id=patient_id,
        charged=charged,
        executed_from=executed_from,
        executed_to=executed_to,
    )


# --- ServiceExecution -------------------------------------------------------


def create_service_execution(
    session: Session,
    visit_id: int,
    data: ServiceExecutionCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> ServiceExecution:
    """Record one service actually performed during a visit.

    The executed price is the point-in-time snapshot: the execution row owns
    its price forever, so later catalog changes never affect it. A service
    executes at most once per visit (``uq_service_executions_org_visit_service``
    is the DB backstop; the pre-check yields the stable error).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, EXECUTIONS_CREATE)

        visit = session.scalar(
            scoped(select(Visit).where(Visit.id == visit_id), Visit, org_id)
        )
        if visit is None:
            raise AppError(ErrorCode.NOT_FOUND, "Visit not found.")
        _load_active_scoped(session, Service, data.service_id, org_id, "Service")

        duplicate = session.scalar(
            select(ServiceExecution).where(
                ServiceExecution.organization_id == org_id,
                ServiceExecution.visit_id == visit.id,
                ServiceExecution.service_id == data.service_id,
            )
        )
        if duplicate is not None:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The service is already executed in this visit.",
            )

        execution = ServiceExecution(
            organization_id=org_id,
            visit_id=visit.id,
            service_id=data.service_id,
            executed_price=data.executed_price,
        )
        session.add(execution)
        try:
            session.flush()
        except IntegrityError as exc:
            # Concurrent race: two different keys, same visit+service. The
            # pre-check above saw no duplicate; the unique index settles it.
            # Surface the approved domain rule as a stable 422, never a 500.
            if _is_duplicate_execution(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The service is already executed in this visit.",
                ) from exc
            raise

        record_event(
            session,
            ctx=resolved,
            entity_type=EXECUTION_ENTITY_TYPE,
            entity_id=str(execution.id),
            action=EXECUTION_CREATED_ACTION,
            after_state={
                "id": execution.id,
                "visit_id": execution.visit_id,
                "service_id": execution.service_id,
                "executed_price": str(execution.executed_price),
            },
        )
        settle_receipt(
            receipt,
            resource_type=EXECUTION_ENTITY_TYPE,
            resource_id=str(execution.id),
            outcome_json=_execution_outcome(execution),
        )

    return execution


# --- shared helpers ---------------------------------------------------------


def _load_active_member(
    session: Session, practitioner_id: int, organization_id: int
):
    from app.organization.models import Practitioner, PractitionerMembership

    membership = session.scalar(
        select(PractitionerMembership).where(
            PractitionerMembership.organization_id == organization_id,
            PractitionerMembership.practitioner_id == practitioner_id,
        )
    )
    practitioner = session.get(Practitioner, practitioner_id)
    if practitioner is None or membership is None:
        raise AppError(ErrorCode.NOT_FOUND, "Practitioner not found.")
    if not practitioner.is_active or not membership.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Practitioner is inactive.")


from app.organization.models import Location  # noqa: E402
