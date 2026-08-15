"""PF5 — Clinical core proofs against real PostgreSQL.

Proves the Patient / Visit / ServiceExecution vertical: tenant isolation at
every level (composite FKs make cross-tenant states structurally impossible),
the appointment-origin rule for visits, the executed-price snapshot semantics,
the one-execution-per-visit rule, permissions, audit provenance, PF4
idempotency, and the migration cycle.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.clinical.models import Patient, ServiceExecution, Visit
from app.clinical.service import (
    OP_EXECUTIONS_CREATE,
    OP_PATIENTS_CREATE,
    OP_VISITS_CREATE,
    create_patient,
    create_service_execution,
    create_visit,
)
from app.commercial.models import Lead
from app.context import default_context
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    EXECUTIONS_CREATE,
    PATIENTS_CREATE,
    PATIENTS_READ,
    VISITS_CREATE,
    VISITS_READ,
)
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
)
from app.idempotency.models import CommandReceipt
from app.idempotency.service import run_idempotent_command
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.service import create_organization
from app.scheduling.models import Appointment, AvailabilityRule
from app.scheduling.service import book_appointment, cancel_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)
RULE_WINDOW = (time(9, 0), time(13, 0))


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed_booking(session, *, organization_id=ORG, name_suffix="1"):
    service = Service(
        organization_id=organization_id,
        name=f"Servicio {name_suffix}",
        duration_minutes=30,
        is_active=True,
    )
    location = Location(
        organization_id=organization_id,
        name=f"Sede {name_suffix}",
        timezone=LIMA,
        is_active=True,
    )
    practitioner = Practitioner(display_name=f"Dra. Ana {name_suffix}", is_active=True)
    lead = Lead(
        organization_id=organization_id,
        full_name=f"Juan Pérez {name_suffix}",
        contact_phone=f"+5199900000{name_suffix}",
        acquisition_source="direct",
    )
    session.add_all([service, location, practitioner, lead])
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=organization_id, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.flush()
    session.add(
        PractitionerCapability(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            service_id=service.id,
            location_id=location.id,
            is_active=True,
        )
    )
    session.add(
        AvailabilityRule(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            location_id=location.id,
            day_of_week=0,
            start_local=RULE_WINDOW[0],
            end_local=RULE_WINDOW[1],
        )
    )
    session.commit()
    return {
        "organization_id": organization_id,
        "lead_id": lead.id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
    }


def seed_actor(session, *, organization_id=ORG, codes=()):
    principal = create_principal(session, display_name="actor", principal_type="human")
    membership = add_membership(session, organization_id=organization_id, principal_id=principal.id)
    role = create_role(
        session, organization_id=organization_id, code=f"role-{principal.id}", name="actor"
    )
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(
        session,
        organization_id=organization_id,
        membership_id=membership.id,
        role_id=role.id,
    )
    values = principal.id
    session.rollback()
    return values


def ctx_for(principal_id, organization_id=ORG, principal_type="human"):
    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal_id,
        principal_type=principal_type,
        request_id="req-clinical",
        correlation_id="corr-clinical",
    )


def make_patient(session, *, dni="12345678", name="Paciente Uno", organization_id=ORG):
    return create_patient(
        session,
        type("D", (), {"full_name": name, "dni": dni, "sexo": "M", "phone": None, "birth_date": None})(),
        ctx=default_context(organization_id),
    )


def book(session, ids, start):
    return book_appointment(
        session,
        ctx=default_context(ids["organization_id"]),
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        location_id=ids["location_id"],
        practitioner_id=ids["practitioner_id"],
        start=start,
    )


# --- Patient ----------------------------------------------------------------


def test_patient_tenant_isolation_and_dni_uniqueness_per_org(session):
    patient = create_patient(
        session,
        type("D", (), {"full_name": "Ana", "dni": "11111111", "sexo": None, "phone": None, "birth_date": None})(),
        ctx=default_context(ORG),
    )
    patient_id = patient.id  # capture before any rollback expires the instance
    org_b = create_organization(session, "Otra Clínica").id
    session.rollback()  # close the refresh transaction create_organization leaves open
    from app.iam.service import provision_system_access
    provision_system_access(session, org_b)
    session.commit()

    # Same DNI in another organization is a different patient (I2-adapted).
    other = create_patient(
        session,
        type("D", (), {"full_name": "Ana", "dni": "11111111", "sexo": None, "phone": None, "birth_date": None})(),
        ctx=default_context(org_b),
    )
    assert patient_id != other.id

    # Duplicate DNI in the SAME organization is rejected with a stable error.
    with pytest.raises(AppError) as exc:
        create_patient(
            session,
            type("D", (), {"full_name": "Ana", "dni": "11111111", "sexo": None, "phone": None, "birth_date": None})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # The DB backstop also refuses the race (partial unique index).
    session.execute(
        text(
            "INSERT INTO patients (organization_id, full_name, dni) VALUES (:o, 'X', '22222222')"
        ),
        {"o": ORG},
    )
    session.commit()
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO patients (organization_id, full_name, dni) VALUES (:o, 'Y', '22222222')"
            ),
            {"o": ORG},
        )
        session.commit()
    session.rollback()
    assert "uq_patients_org_dni" in str(exc.value)

    # Two org-scoped patients + the raw backstop insert = three rows.
    assert session.scalar(select(func.count()).select_from(Patient)) == 3
    session.rollback()


def test_patient_search_and_read(session):
    create_patient(
        session,
        type("D", (), {"full_name": "María Pérez", "dni": "33333333", "sexo": "F", "phone": None, "birth_date": None})(),
        ctx=default_context(ORG),
    )
    from app.clinical.service import get_patient, list_patients

    found = list_patients(session, ctx=default_context(ORG), search="Mar")
    assert len(found) == 1
    by_dni = list_patients(session, ctx=default_context(ORG), search="33333333")
    assert len(by_dni) == 1
    assert get_patient(session, found[0].id, ctx=default_context(ORG)).dni == "33333333"


def test_patient_cross_org_read_is_not_found(session):
    org_b = create_organization(session, "Otra Clínica").id
    session.rollback()  # close the refresh transaction create_organization leaves open
    from app.iam.service import provision_system_access
    provision_system_access(session, org_b)
    session.commit()
    patient = create_patient(
        session,
        type("D", (), {"full_name": "Ana", "dni": "44444444", "sexo": None, "phone": None, "birth_date": None})(),
        ctx=default_context(org_b),
    )
    from app.clinical.service import get_patient

    with pytest.raises(AppError) as exc:
        get_patient(session, patient.id, ctx=default_context(ORG))
    assert exc.value.code == ErrorCode.NOT_FOUND
    session.rollback()


# --- Visit ------------------------------------------------------------------


def test_visit_from_confirmed_appointment_derives_practitioner_and_location(session):
    ids = seed_booking(session)
    patient = make_patient(session)
    appointment = book(session, ids, utc_of(9))

    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient.id,
                "appointment_id": appointment.id,
                "practitioner_id": None,
                "location_id": None,
            },
        )(),
        ctx=default_context(ORG),
    )
    assert visit.appointment_id == appointment.id
    assert visit.practitioner_id == ids["practitioner_id"]
    assert visit.location_id == ids["location_id"]
    assert visit.started_at is not None


def test_visit_rejects_cancelled_appointment_origin(session):
    ids = seed_booking(session)
    patient = make_patient(session)
    appointment = book(session, ids, utc_of(9))
    cancel_appointment(session, appointment.id, ctx=default_context(ORG))

    with pytest.raises(AppError) as exc:
        create_visit(
            session,
            type(
                "D",
                (),
                {
                    "patient_id": patient.id,
                    "appointment_id": appointment.id,
                    "practitioner_id": None,
                    "location_id": None,
                },
            )(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.ENTITY_INACTIVE
    session.rollback()


def test_visit_walkin_requires_practitioner_and_location(session):
    patient = make_patient(session)
    with pytest.raises(AppError) as exc:
        create_visit(
            session,
            type(
                "D",
                (),
                {"patient_id": patient.id, "appointment_id": None, "practitioner_id": None, "location_id": None},
            )(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()


def test_visit_cannot_link_resources_from_another_org(session):
    org_b = create_organization(session, "Otra Clínica").id
    session.rollback()  # close the refresh transaction create_organization leaves open
    from app.iam.service import provision_system_access
    provision_system_access(session, org_b)
    session.commit()
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")
    patient_a = make_patient(session)  # org A

    # A visit in org A cannot reference org B's appointment (composite FK).
    appointment_b = book(session, ids_b, utc_of(9))
    with pytest.raises(AppError) as exc:
        create_visit(
            session,
            type(
                "D",
                (),
                {
                    "patient_id": patient_a.id,
                    "appointment_id": appointment_b.id,
                    "practitioner_id": None,
                    "location_id": None,
                },
            )(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.NOT_FOUND  # the appointment is not in org A
    session.rollback()

    # DB-level proof: raw insert of a cross-org appointment link is rejected.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO visits (organization_id, patient_id, appointment_id, practitioner_id, location_id) "
                "VALUES (:org, :patient, :appt, :practitioner, :location)"
            ),
            {
                "org": ORG,
                "patient": patient_a.id,
                "appt": appointment_b.id,
                "practitioner": ids_b["practitioner_id"],
                "location": ids_b["location_id"],
            },
        )
        session.commit()
    session.rollback()
    assert "fk_visits_organization_appointment" in str(exc.value)

    # And a cross-org patient link is equally impossible.
    patient_b = create_patient(
        session,
        type("D", (), {"full_name": "B", "dni": "55555555", "sexo": None, "phone": None, "birth_date": None})(),
        ctx=default_context(org_b),
    )
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO visits (organization_id, patient_id, appointment_id, practitioner_id, location_id) "
                "VALUES (:org, :patient, NULL, :practitioner, :location)"
            ),
            {
                "org": ORG,
                "patient": patient_b.id,
                "practitioner": ids_b["practitioner_id"],
                "location": ids_b["location_id"],
            },
        )
        session.commit()
    session.rollback()
    assert "fk_visits_organization_patient" in str(exc.value)


# --- ServiceExecution -------------------------------------------------------


def test_execution_links_visit_and_canonical_service_with_snapshot(session):
    ids = seed_booking(session)
    patient = make_patient(session)
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient.id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )

    execution = create_service_execution(
        session,
        visit.id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150.00")})(),
        ctx=default_context(ORG),
    )
    assert execution.visit_id == visit.id
    assert execution.service_id == ids["service_id"]
    assert execution.executed_price == Decimal("150.00")


def test_executed_price_snapshot_survives_catalog_changes(session):
    ids = seed_booking(session)
    patient = make_patient(session)
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient.id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )
    execution = create_service_execution(
        session,
        visit.id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150.00")})(),
        ctx=default_context(ORG),
    )

    # The catalog changes afterwards: the recorded price must not move.
    service = session.get(Service, ids["service_id"])
    service.name = "Limpieza premium"
    service.duration_minutes = 45
    session.commit()

    fresh = session.get(ServiceExecution, execution.id)
    assert fresh.executed_price == Decimal("150.00")


def test_multiple_executions_per_visit_and_duplicate_rule(session):
    ids = seed_booking(session)
    session.add(Service(organization_id=ORG, name="Evaluación", duration_minutes=15, is_active=True))
    session.commit()
    second_service_id = session.scalar(
        select(Service.id).where(Service.name == "Evaluación")
    )
    session.rollback()

    patient = make_patient(session)
    patient_id = patient.id  # capture before any rollback expires the instance
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient_id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )
    visit_id = visit.id  # capture before any rollback expires the instance
    create_service_execution(
        session,
        visit_id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150")})(),
        ctx=default_context(ORG),
    )
    create_service_execution(
        session,
        visit_id,
        type("D", (), {"service_id": second_service_id, "executed_price": Decimal("80")})(),
        ctx=default_context(ORG),
    )

    # Same service twice in one visit → stable domain error.
    with pytest.raises(AppError) as exc:
        create_service_execution(
            session,
            visit_id,
            type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("1")})(),
            ctx=default_context(ORG),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    session.rollback()

    # DB backstop refuses the duplicate too.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO service_executions (organization_id, visit_id, service_id, executed_price) "
                "VALUES (:o, :v, :s, 1.00)"
            ),
            {"o": ORG, "v": visit_id, "s": ids["service_id"]},
        )
        session.commit()
    session.rollback()
    assert "uq_service_executions_org_visit_service" in str(exc.value)

    # A different visit may execute the same service again (legacy rule).
    other_visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient_id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )
    create_service_execution(
        session,
        other_visit.id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150")})(),
        ctx=default_context(ORG),
    )
    assert session.scalar(select(func.count()).select_from(ServiceExecution)) == 3
    session.rollback()


def test_cross_tenant_execution_rejected_by_db(session):
    ids = seed_booking(session)
    org_b = create_organization(session, "Otra Clínica").id
    session.rollback()  # close the refresh transaction create_organization leaves open
    from app.iam.service import provision_system_access
    provision_system_access(session, org_b)
    session.commit()
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")
    patient = make_patient(session)
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient.id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )

    # Raw insert: org A visit + org B service → composite FK violation.
    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO service_executions (organization_id, visit_id, service_id, executed_price) "
                "VALUES (:o, :v, :s, 1.00)"
            ),
            {"o": ORG, "v": visit.id, "s": ids_b["service_id"]},
        )
        session.commit()
    session.rollback()
    assert "fk_service_executions_organization_service" in str(exc.value)


def test_visit_detail_includes_executions(session):
    ids = seed_booking(session)
    patient = make_patient(session)
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient.id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )
    create_service_execution(
        session,
        visit.id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150")})(),
        ctx=default_context(ORG),
    )
    from app.clinical.service import get_visit

    detail = get_visit(session, visit.id, ctx=default_context(ORG))
    assert len(detail.executions) == 1
    assert detail.executions[0].service.name == "Servicio 1"


# --- permissions / audit / idempotency --------------------------------------


def test_clinical_commands_enforce_permissions(session):
    ids = seed_booking(session)
    no_perm = seed_actor(session, codes=())
    ctx = ctx_for(no_perm)
    patient_data = type(
        "D", (), {"full_name": "Ana", "dni": "66666666", "sexo": None, "phone": None, "birth_date": None}
    )()

    with pytest.raises(AppError) as exc:
        create_patient(session, patient_data, ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Patient)) == 0
    session.rollback()

    patient = make_patient(session)
    patient_id = patient.id  # capture before any rollback expires the instance
    with pytest.raises(AppError) as exc:
        create_visit(
            session,
            type(
                "D",
                (),
                {
                    "patient_id": patient_id,
                    "appointment_id": None,
                    "practitioner_id": ids["practitioner_id"],
                    "location_id": ids["location_id"],
                },
            )(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient_id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )
    with pytest.raises(AppError) as exc:
        create_service_execution(
            session,
            visit.id,
            type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("1")})(),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()


def test_audit_provenance_for_clinical_creates(session):
    ids = seed_booking(session)
    actor = seed_actor(
        session,
        codes=(PATIENTS_CREATE, VISITS_CREATE, EXECUTIONS_CREATE),
    )
    ctx = ctx_for(actor)

    patient = create_patient(
        session,
        type("D", (), {"full_name": "Ana", "dni": "77777777", "sexo": None, "phone": None, "birth_date": None})(),
        ctx=ctx,
    )
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient.id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=ctx,
    )
    create_service_execution(
        session,
        visit.id,
        type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150")})(),
        ctx=ctx,
    )

    rows = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    session.rollback()
    assert [row.action for row in rows] == [
        "patient.created",
        "visit.created",
        "service_execution.created",
    ]
    assert all(row.organization_id == ORG for row in rows)
    assert all(row.actor_id == str(actor) for row in rows)
    assert all(row.correlation_id == "corr-clinical" for row in rows)


def test_clinical_creates_are_idempotent(session):
    ids = seed_booking(session)
    ctx = default_context(ORG)
    patient_payload = type(
        "D", (), {"full_name": "Ana", "dni": "88888888", "sexo": None, "phone": None, "birth_date": None}
    )()

    first = run_idempotent_command(
        session,
        operation=create_patient,
        operation_name=OP_PATIENTS_CREATE,
        key="key-patient-1",
        ctx=ctx,
        params={"full_name": "Ana", "dni": "88888888"},
        data=patient_payload,
    )
    assert first.replayed is False
    patient_id = first.result.id  # capture before any replay rollback expires it

    replay = run_idempotent_command(
        session,
        operation=create_patient,
        operation_name=OP_PATIENTS_CREATE,
        key="key-patient-1",
        ctx=ctx,
        params={"full_name": "Ana", "dni": "88888888"},
        data=patient_payload,
    )
    assert replay.replayed is True
    assert replay.outcome["resource_id"] == str(first.result.id)
    assert session.scalar(select(func.count()).select_from(Patient)) == 1
    session.rollback()  # the count query autobegins a transaction

    visit_payload = type(
        "D",
        (),
        {
            "patient_id": patient_id,
            "appointment_id": None,
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
        },
    )()
    visit_params = {"patient_id": patient_id, "appointment_id": None,
                    "practitioner_id": ids["practitioner_id"], "location_id": ids["location_id"]}
    v1 = run_idempotent_command(
        session,
        operation=create_visit,
        operation_name=OP_VISITS_CREATE,
        key="key-visit-1",
        ctx=ctx,
        params=visit_params,
        data=visit_payload,
    )
    v2 = run_idempotent_command(
        session,
        operation=create_visit,
        operation_name=OP_VISITS_CREATE,
        key="key-visit-1",
        ctx=ctx,
        params=visit_params,
        data=visit_payload,
    )
    visit_id = v1.result.id  # capture immediately (v2's replay rollback expires it)
    assert v2.replayed is True
    assert v2.outcome["resource_id"] == str(v1.result.id)
    assert session.scalar(select(func.count()).select_from(Visit)) == 1
    session.rollback()  # the count query autobegins a transaction

    exec_payload = type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150")})()
    e1 = run_idempotent_command(
        session,
        operation=create_service_execution,
        operation_name=OP_EXECUTIONS_CREATE,
        key="key-exec-1",
        ctx=ctx,
        params={"visit_id": visit_id, "service_id": ids["service_id"], "executed_price": "150"},
        visit_id=visit_id,
        data=exec_payload,
    )
    e2 = run_idempotent_command(
        session,
        operation=create_service_execution,
        operation_name=OP_EXECUTIONS_CREATE,
        key="key-exec-1",
        ctx=ctx,
        params={"visit_id": visit_id, "service_id": ids["service_id"], "executed_price": "150"},
        visit_id=visit_id,
        data=exec_payload,
    )
    exec_id = e1.result.id  # capture immediately (e2's replay rollback expires it)
    assert e2.replayed is True
    assert e2.outcome["resource_id"] == str(exec_id)
    assert session.scalar(select(func.count()).select_from(ServiceExecution)) == 1
    assert session.scalar(select(func.count()).select_from(CommandReceipt)) == 3
    session.rollback()


# --- HTTP surface ------------------------------------------------------------


@pytest.fixture
def api_app(migrated_engine):
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    return app, maker


@pytest.fixture
def client(api_app):
    app, _ = api_app
    return TestClient(app, raise_server_exceptions=False)


def test_clinical_http_journey(client, session):
    ids = seed_booking(session)

    created = client.post(
        "/patients",
        json={"full_name": "Ana Torres", "dni": "99999999", "sexo": "F"},
        headers={"Idempotency-Key": "http-patient-1"},
    )
    assert created.status_code == 201, created.text
    patient_id = created.json()["id"]
    replay = client.post(
        "/patients",
        json={"full_name": "Ana Torres", "dni": "99999999", "sexo": "F"},
        headers={"Idempotency-Key": "http-patient-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == patient_id
    assert replay.headers.get("Idempotent-Replay") == "true"

    listed = client.get("/patients")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    visit = client.post(
        "/visits",
        json={
            "patient_id": patient_id,
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
        },
        headers={"Idempotency-Key": "http-visit-1"},
    )
    assert visit.status_code == 201, visit.text
    visit_id = visit.json()["id"]
    assert visit.json()["patient_name"] == "Ana Torres"

    execution = client.post(
        f"/visits/{visit_id}/executions",
        json={"service_id": ids["service_id"], "executed_price": "150.00"},
        headers={"Idempotency-Key": "http-exec-1"},
    )
    assert execution.status_code == 201, execution.text
    assert execution.json()["service_name"] == "Servicio 1"

    detail = client.get(f"/visits/{visit_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["executions"]) == 1
    assert body["executions"][0]["executed_price"] == "150.00"

    visits = client.get("/visits", params={"patient_id": patient_id})
    assert len(visits.json()) == 1

    invalid_dni = client.post(
        "/patients", json={"full_name": "X", "dni": "123"}
    )
    assert invalid_dni.status_code == 422

    bad_sexo = client.post(
        "/patients", json={"full_name": "X", "dni": "11112222", "sexo": "Z"}
    )
    assert bad_sexo.status_code == 422


# --- repair-pass proofs (review ISSUEs 1-3) ---------------------------------


def test_concurrent_duplicate_execution_settles_as_422_not_500(migrated_engine, session):
    """Two different idempotency keys, same visit+service, racing: the unique
    index settles it and the loser gets the approved stable 422 — never a
    raw 23505/500 (legacy defect fixed)."""
    import threading

    from app.clinical.service import list_visit_executions

    ids = seed_booking(session)
    patient = make_patient(session)
    patient_id = patient.id
    visit = create_visit(
        session,
        type(
            "D",
            (),
            {
                "patient_id": patient_id,
                "appointment_id": None,
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
            },
        )(),
        ctx=default_context(ORG),
    )
    visit_id = visit.id

    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt(key):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            create_service_execution(
                db,
                visit_id,
                type("D", (), {"service_id": ids["service_id"], "executed_price": Decimal("150")})(),
                ctx=default_context(ORG),
            )
            with guard:
                outcomes.append(("committed", key))
        except AppError as exc:
            db.rollback()
            with guard:
                outcomes.append(("app_error", exc.code, key))
        finally:
            db.close()

    threads = [
        threading.Thread(target=attempt, args=("race-key-a",)),
        threading.Thread(target=attempt, args=("race-key-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    committed = [o for o in outcomes if o[0] == "committed"]
    rejected = [o for o in outcomes if o[0] == "app_error"]
    assert len(committed) == 1, outcomes
    assert len(rejected) == 1, outcomes
    assert rejected[0][1] == ErrorCode.INVALID_INPUT, outcomes

    db = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)()
    rows = list_visit_executions(db, visit_id, ctx=default_context(ORG))
    db.close()
    assert len(rows) == 1


def test_visit_create_rejects_mixed_origin(client, session):
    ids = seed_booking(session)
    patient = make_patient(session)
    patient_id = patient.id
    response = client.post(
        "/visits",
        json={
            "patient_id": patient_id,
            "appointment_id": 999,
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
        },
    )
    assert response.status_code == 422, response.text

    missing = client.post(
        "/visits",
        json={"patient_id": patient_id},
    )
    assert missing.status_code == 422, missing.text


def test_executions_list_endpoint(client, session):
    ids = seed_booking(session)
    patient = make_patient(session)
    patient_id = patient.id
    visit = client.post(
        "/visits",
        json={
            "patient_id": patient_id,
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
        },
        headers={"Idempotency-Key": "list-exec-v1"},
    )
    visit_id = visit.json()["id"]
    client.post(
        f"/visits/{visit_id}/executions",
        json={"service_id": ids["service_id"], "executed_price": "150.00"},
        headers={"Idempotency-Key": "list-exec-e1"},
    )
    listed = client.get(f"/visits/{visit_id}/executions")
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["service_name"] == "Servicio 1"
    assert rows[0]["executed_price"] == "150.00"

    missing_visit = client.get("/visits/999999/executions")
    assert missing_visit.status_code == 404
