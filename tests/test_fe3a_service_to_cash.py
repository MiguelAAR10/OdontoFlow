"""FE3A Service-to-Cash V1 contract tests.

These tests are intentionally written before the implementation.  The economic
surface remains the sole money authority; FE3A only enriches its projections and
adds typed reconciliation/follow-up state around the existing ledgers.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import threading
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.catalog.models import Service
from app.clinical.models import Patient, ServiceExecution, Visit
from app.clinical.schemas import VisitCreate
from app.clinical.service import create_visit
from app.commercial.models import Lead
from app.context import default_context
from app.db import get_db
from app.economics.models import Charge, ChargeFollowUp, Payment
from app.economics import schemas
from app.economics.schemas import ChargeCreate
from app.economics.service import (
    OP_FOLLOW_UPS_CLOSE,
    OP_FOLLOW_UPS_CREATE,
    OP_FOLLOW_UPS_RESCHEDULE,
    OP_PAYMENTS_CREATE,
    close_follow_up,
    create_charge,
    create_payment,
    list_follow_ups,
    open_follow_up,
    reschedule_follow_up,
)
from app.errors import AppError, ErrorCode
from app.idempotency.service import run_idempotent_command
from app.iam.models import Permission, Role, RolePermission
from app.organization.models import Location, Practitioner, PractitionerMembership
from app.organization.service import create_organization
from app.scheduling.models import Appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from conftest import AUTH_HEADERS

LIMA = "America/Lima"
LIMA_TZ = ZoneInfo(LIMA)
UTC = timezone.utc


def _schema(name):
    schema = getattr(schemas, name, None)
    assert schema is not None, f"missing FE3A schema: {name}"
    return schema


@pytest.mark.parametrize(
    "method",
    ("efectivo", "tarjeta", "yape", "plin", "transferencia", "link_pago"),
)
def test_payment_create_accepts_the_frozen_method_codes(method):
    payload = _schema("PaymentCreate")(
        amount=Decimal("10.00"),
        method=method,
        reference=" op-001 " if method in {"yape", "plin", "transferencia"} else None,
    )

    assert payload.method == method


@pytest.mark.parametrize("method", ("yape", "plin", "transferencia"))
def test_payment_create_requires_a_digital_operation_reference(method):
    with pytest.raises(ValidationError, match="reference is required"):
        _schema("PaymentCreate")(amount=Decimal("10.00"), method=method)


@pytest.mark.parametrize("method", ("Yape", "YAPE", "cash", ""))
def test_payment_create_rejects_display_labels_and_unknown_methods(method):
    kwargs = {"amount": Decimal("10.00"), "method": method}
    if method == "Yape":
        kwargs["reference"] = "op-001"
    with pytest.raises(ValidationError):
        _schema("PaymentCreate")(**kwargs)


def test_payment_create_strips_reference_without_changing_case():
    payload = _schema("PaymentCreate")(
        amount=Decimal("10.00"), method="yape", reference=" Op-AbC-9 "
    )

    assert payload.reference == "Op-AbC-9"


def test_payment_verify_has_only_reconciliation_note():
    payment_verify = _schema("PaymentVerify")
    payload = payment_verify(reconciliation_note="statement checked")

    assert payload.reconciliation_note == "statement checked"
    with pytest.raises(ValidationError):
        payment_verify(verified_at="2026-09-06T10:00:00Z")


def test_follow_up_commands_are_strict_and_keep_the_promised_date_explicit():
    follow_up_create = _schema("ChargeFollowUpCreate")
    follow_up_reschedule = _schema("ChargeFollowUpReschedule")
    follow_up_close = _schema("ChargeFollowUpClose")
    assert follow_up_create(next_follow_up_on=date(2026, 9, 7)).next_follow_up_on == date(
        2026, 9, 7
    )
    assert follow_up_reschedule(next_follow_up_on=date(2026, 9, 8)).note is None
    assert follow_up_close(note="debt remains owed").note == "debt remains owed"
    with pytest.raises(ValidationError):
        follow_up_create(next_follow_up_on=date(2026, 9, 7), assignee_id=1)


def test_fe3a_read_and_reconciliation_routes_are_in_the_contract():
    paths = create_app().openapi()["paths"]

    assert "/executions" in paths
    assert "/payments" in paths
    assert "/payments/{payment_id}/verify" in paths
    assert "/follow-ups" in paths
    assert "/charges/{charge_id}/follow-ups" in paths


@pytest.fixture
def fe3a_client(migrated_engine):
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    with TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS) as client:
        yield client


def _seed_chain(session, *, organization_id=ORG, suffix="a"):
    """Create the smallest real clinical chain used by FE3A proofs."""
    service = Service(
        organization_id=organization_id,
        name=f"FE3A service {suffix}",
        duration_minutes=30,
        is_active=True,
    )
    location = Location(
        organization_id=organization_id,
        name=f"FE3A location {suffix}",
        timezone=LIMA,
        is_active=True,
    )
    practitioner = Practitioner(display_name=f"FE3A practitioner {suffix}", is_active=True)
    lead = Lead(
        organization_id=organization_id,
        full_name=f"FE3A lead {suffix}",
        contact_phone=f"+5198{organization_id:02d}{abs(hash(suffix)) % 100000:05d}",
        acquisition_source="direct",
    )
    session.add_all([service, location, practitioner, lead])
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            is_active=True,
        )
    )
    patient = Patient(
        organization_id=organization_id,
        full_name=f"FE3A patient {suffix}",
        dni=f"7{organization_id:01d}{abs(hash(suffix)) % 1000000:06d}",
    )
    session.add(patient)
    session.flush()
    visit = Visit(
        organization_id=organization_id,
        patient_id=patient.id,
        practitioner_id=practitioner.id,
        location_id=location.id,
    )
    session.add(visit)
    session.flush()
    execution = ServiceExecution(
        organization_id=organization_id,
        visit_id=visit.id,
        service_id=service.id,
        executed_price=Decimal("150.00"),
    )
    session.add(execution)
    session.commit()
    return {
        "organization_id": organization_id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
        "lead_id": lead.id,
        "patient_id": patient.id,
        "visit_id": visit.id,
        "execution_id": execution.id,
    }


def _make_charge(session, ids, amount=Decimal("150.00")):
    return create_charge(
        session,
        ids["execution_id"],
        ChargeCreate(amount=amount),
        organization_id=ids["organization_id"],
    )


def _today() -> date:
    return datetime.now(LIMA_TZ).date()


def test_appointment_reads_project_patient_identity(fe3a_client, session):
    ids = _seed_chain(session, suffix="appointment-patient")
    appointment = Appointment(
        organization_id=ORG,
        lead_id=ids["lead_id"],
        patient_id=ids["patient_id"],
        service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"],
        start_utc=datetime(2026, 9, 8, 15, 0, tzinfo=UTC),
        end_utc=datetime(2026, 9, 8, 15, 30, tzinfo=UTC),
        state="confirmed",
    )
    session.add(appointment)
    session.commit()

    listed = fe3a_client.get("/appointments")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == appointment.id)
    assert row["patient_id"] == ids["patient_id"]
    assert row["patient_name"] == "FE3A patient appointment-patient"

    detail = fe3a_client.get(f"/appointments/{appointment.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["patient_id"] == ids["patient_id"]


def test_projection_filters_and_reconciliation_http(fe3a_client, session):
    ids = _seed_chain(session, suffix="projection")

    uncharged = fe3a_client.get(
        "/executions",
        params={"visit_id": ids["visit_id"], "charged": "false"},
    )
    assert uncharged.status_code == 200, uncharged.text
    assert uncharged.json()[0] == {
        "id": ids["execution_id"],
        "visit_id": ids["visit_id"],
        "service_id": ids["service_id"],
        "service_name": "FE3A service projection",
        "executed_price": "150.00",
        "executed_at": uncharged.json()[0]["executed_at"],
        "charge_id": None,
        "patient_id": ids["patient_id"],
        "patient_name": "FE3A patient projection",
        "location_id": ids["location_id"],
    }

    charge_response = fe3a_client.post(
        f"/executions/{ids['execution_id']}/charges",
        json={"amount": "150.00"},
        headers={**AUTH_HEADERS, "Idempotency-Key": str(uuid4())},
    )
    assert charge_response.status_code == 201, charge_response.text
    charge = charge_response.json()
    charge_id = charge["id"]
    visit_detail = fe3a_client.get(f"/visits/{ids['visit_id']}")
    assert visit_detail.status_code == 200, visit_detail.text
    assert visit_detail.json()["executions"][0]["charge_id"] == charge_id
    for key, value in {
        "visit_id": ids["visit_id"],
        "patient_id": ids["patient_id"],
        "patient_name": "FE3A patient projection",
        "service_id": ids["service_id"],
        "service_name": "FE3A service projection",
        "location_id": ids["location_id"],
        "location_name": "FE3A location projection",
        "practitioner_id": ids["practitioner_id"],
        "practitioner_name": "FE3A practitioner projection",
        "executed_at": uncharged.json()[0]["executed_at"],
    }.items():
        assert charge[key] == value

    for params in (
        {"execution_id": ids["execution_id"]},
        {"patient_id": ids["patient_id"]},
        {"location_id": ids["location_id"]},
        {"visit_id": ids["visit_id"]},
        {"created_from": "2000-01-01", "created_to": "2999-01-01"},
    ):
        response = fe3a_client.get("/charges", params=params)
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()] == [charge_id]
    assert fe3a_client.get("/charges", params={"status": "unpaid"}).json()[0]["id"] == charge_id
    invalid_status = fe3a_client.get("/charges", params={"status": "unknown"})
    assert invalid_status.status_code == 422

    partial = fe3a_client.post(
        f"/charges/{charge_id}/payments",
        json={
            "amount": "50.00",
            "method": "yape",
            "reference": " op-projection-1 ",
            "receiver": "Caja",
        },
        headers={**AUTH_HEADERS, "Idempotency-Key": str(uuid4())},
    )
    assert partial.status_code == 201, partial.text
    payment = partial.json()
    assert payment["reference"] == "op-projection-1"
    assert payment["verification_status"] == "unverified"
    assert fe3a_client.get("/charges", params={"status": "partial"}).json()[0]["id"] == charge_id
    assert fe3a_client.get(
        "/executions", params={"charged": "true"}
    ).json()[0]["charge_id"] == charge_id
    assert fe3a_client.get("/executions", params={"charged": "false"}).json() == []
    assert fe3a_client.get(
        "/payments", params={"method": "yape", "verification_status": "unverified"}
    ).json()[0]["id"] == payment["id"]

    verified = fe3a_client.post(
        f"/payments/{payment['id']}/verify",
        json={"reconciliation_note": "statement checked"},
        headers={**AUTH_HEADERS, "Idempotency-Key": str(uuid4())},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verification_status"] == "verified"
    assert verified.json()["verified_at"] is not None
    second_verify = fe3a_client.post(
        f"/payments/{payment['id']}/verify",
        json={},
    )
    assert second_verify.status_code == 422
    assert second_verify.json()["error"]["code"] == "INVALID_INPUT"

    final = fe3a_client.post(
        f"/charges/{charge_id}/payments",
        json={"amount": "100.00", "method": "efectivo"},
        headers={**AUTH_HEADERS, "Idempotency-Key": str(uuid4())},
    )
    assert final.status_code == 201, final.text
    assert fe3a_client.get("/charges", params={"status": "paid"}).json()[0]["id"] == charge_id


def test_payment_reference_uniqueness_is_tenant_scoped_and_idempotent(session):
    ids = _seed_chain(session, suffix="payments-a")
    charge = _make_charge(session, ids)
    charge_id = charge.id
    ctx = default_context(ids["organization_id"])
    data = schemas.PaymentCreate(
        amount=Decimal("10.00"), method="transferencia", reference="OP-A-1"
    )
    key = str(uuid4())
    first = run_idempotent_command(
        session,
        operation=create_payment,
        operation_name=OP_PAYMENTS_CREATE,
        key=key,
        ctx=ctx,
        params={"charge_id": charge_id, **data.model_dump()},
        charge_id=charge_id,
        data=data,
    )
    replay = run_idempotent_command(
        session,
        operation=create_payment,
        operation_name=OP_PAYMENTS_CREATE,
        key=key,
        ctx=ctx,
        params={"charge_id": charge_id, **data.model_dump()},
        charge_id=charge_id,
        data=data,
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.outcome["reference"] == "OP-A-1"
    session.rollback()

    changed = schemas.PaymentCreate(
        amount=Decimal("10.00"), method="transferencia", reference="OP-A-2"
    )
    with pytest.raises(AppError) as reused:
        run_idempotent_command(
            session,
            operation=create_payment,
            operation_name=OP_PAYMENTS_CREATE,
            key=key,
            ctx=ctx,
            params={"charge_id": charge_id, **changed.model_dump()},
            charge_id=charge_id,
            data=changed,
        )
    assert reused.value.code == ErrorCode.IDEMPOTENCY_KEY_REUSED
    session.rollback()

    with pytest.raises(AppError, match="operation code"):
        create_payment(
            session,
            charge_id,
            data,
            organization_id=ids["organization_id"],
        )
    session.rollback()

    org_b = create_organization(session, "FE3A other org").id
    ids_b = _seed_chain(session, organization_id=org_b, suffix="payments-b")
    charge_b = _make_charge(session, ids_b)
    payment_b = create_payment(
        session,
        charge_b.id,
        data,
        organization_id=org_b,
    )
    assert payment_b.organization_id == org_b


def test_fe3a_migration_constraints_are_structural(migrated_engine):
    with migrated_engine.connect() as conn:
        constraints = {
            row.conname: row.convalidated
            for row in conn.execute(
                text(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conrelid IN ('payments'::regclass, 'charge_follow_ups'::regclass)"
                )
            )
        }
        indexes = {
            row.relname: (row.indisunique, row.predicate or "")
            for row in conn.execute(
                text(
                    "SELECT index_class.relname, pg_index.indisunique, "
                    "pg_get_expr(pg_index.indpred, pg_index.indrelid) AS predicate "
                    "FROM pg_index "
                    "JOIN pg_class index_class ON index_class.oid = pg_index.indexrelid "
                    "WHERE index_class.relname IN ("
                    "'uq_visits_org_appointment', 'uq_payments_org_method_reference', "
                    "'uq_charge_follow_ups_org_charge_open', 'ix_charge_follow_ups_org_due')"
                )
            )
        }
    assert constraints["ck_payments_method"] is True
    assert constraints["ck_payments_verification_status"] is True
    assert constraints["ck_payments_verified_at_consistency"] is True
    assert constraints["ck_payments_digital_reference"] is False
    assert constraints["ck_charge_follow_ups_state"] is True
    assert constraints["ck_charge_follow_ups_close_reason"] is True
    assert constraints["ck_charge_follow_ups_closure"] is True
    assert constraints["fk_charge_follow_ups_organization_charge"] is True
    assert indexes["uq_visits_org_appointment"][0] is True
    assert indexes["uq_payments_org_method_reference"][0] is True
    assert indexes["uq_charge_follow_ups_org_charge_open"][0] is True
    assert "state" in indexes["uq_charge_follow_ups_org_charge_open"][1]
    assert "open" in indexes["uq_charge_follow_ups_org_charge_open"][1]
    assert "state" in indexes["ix_charge_follow_ups_org_due"][1]
    assert "open" in indexes["ix_charge_follow_ups_org_due"][1]


def test_follow_up_idempotency_filters_and_atomic_settlement(session):
    ids = _seed_chain(session, suffix="follow-up")
    charge = _make_charge(session, ids)
    charge_id = charge.id
    today = _today()
    data = schemas.ChargeFollowUpCreate(next_follow_up_on=today, note="call today")
    ctx = default_context(ids["organization_id"])
    key = str(uuid4())
    opened = run_idempotent_command(
        session,
        operation=open_follow_up,
        operation_name=OP_FOLLOW_UPS_CREATE,
        key=key,
        ctx=ctx,
        params={"charge_id": charge_id, **data.model_dump()},
        charge_id=charge_id,
        data=data,
    )
    replay = run_idempotent_command(
        session,
        operation=open_follow_up,
        operation_name=OP_FOLLOW_UPS_CREATE,
        key=key,
        ctx=ctx,
        params={"charge_id": charge_id, **data.model_dump()},
        charge_id=charge_id,
        data=data,
    )
    assert opened.replayed is False
    assert replay.replayed is True
    follow_up_id = opened.result.id
    assert len(list_follow_ups(session, organization_id=ORG, active=True)) == 1
    session.rollback()

    partial = create_payment(
        session,
        charge_id,
        schemas.PaymentCreate(amount=Decimal("50.00"), method="efectivo"),
        organization_id=ORG,
    )
    assert partial.amount == Decimal("50.00")
    assert list_follow_ups(session, organization_id=ORG, active=True)[0].id == follow_up_id
    session.rollback()

    reschedule_data = schemas.ChargeFollowUpReschedule(
        next_follow_up_on=today + timedelta(days=1), note="tomorrow"
    )
    reschedule_key = str(uuid4())
    rescheduled = run_idempotent_command(
        session,
        operation=reschedule_follow_up,
        operation_name=OP_FOLLOW_UPS_RESCHEDULE,
        key=reschedule_key,
        ctx=ctx,
        params={"follow_up_id": follow_up_id, **reschedule_data.model_dump()},
        follow_up_id=follow_up_id,
        data=reschedule_data,
    )
    assert rescheduled.result.next_follow_up_on == today + timedelta(days=1)
    assert run_idempotent_command(
        session,
        operation=reschedule_follow_up,
        operation_name=OP_FOLLOW_UPS_RESCHEDULE,
        key=reschedule_key,
        ctx=ctx,
        params={"follow_up_id": follow_up_id, **reschedule_data.model_dump()},
        follow_up_id=follow_up_id,
        data=reschedule_data,
    ).replayed

    close_data = schemas.ChargeFollowUpClose(note="operator called")
    close_key = str(uuid4())
    closed = run_idempotent_command(
        session,
        operation=close_follow_up,
        operation_name=OP_FOLLOW_UPS_CLOSE,
        key=close_key,
        ctx=ctx,
        params={"follow_up_id": follow_up_id, **close_data.model_dump()},
        follow_up_id=follow_up_id,
        data=close_data,
    )
    assert closed.result.close_reason == "closed_by_operator"
    assert run_idempotent_command(
        session,
        operation=close_follow_up,
        operation_name=OP_FOLLOW_UPS_CLOSE,
        key=close_key,
        ctx=ctx,
        params={"follow_up_id": follow_up_id, **close_data.model_dump()},
        follow_up_id=follow_up_id,
        data=close_data,
    ).replayed

    charge_before = session.get(Charge, charge_id)
    assert charge_before.amount == Decimal("150.00")
    assert list_follow_ups(session, organization_id=ORG, active=True) == []
    session.rollback()

    reopened = open_follow_up(
        session,
        charge_id,
        schemas.ChargeFollowUpCreate(next_follow_up_on=today),
        organization_id=ORG,
    )
    assert reopened.id != follow_up_id
    settled_payment = create_payment(
        session,
        charge_id,
        schemas.PaymentCreate(amount=Decimal("100.00"), method="efectivo"),
        organization_id=ORG,
    )
    assert settled_payment.amount == Decimal("100.00")
    settled = session.scalar(select(ChargeFollowUp).where(ChargeFollowUp.id == reopened.id))
    assert settled.state == "closed"
    assert settled.close_reason == "settled"
    assert list_follow_ups(session, organization_id=ORG, active=True) == []
    session.rollback()

    with pytest.raises(AppError, match="fully paid"):
        open_follow_up(
            session,
            charge_id,
            schemas.ChargeFollowUpCreate(next_follow_up_on=today),
            organization_id=ORG,
        )
    session.rollback()

    ids_rollback = _seed_chain(session, suffix="follow-up-rollback")
    charge_rollback = _make_charge(session, ids_rollback)
    follow_up_rollback = open_follow_up(
        session,
        charge_rollback.id,
        schemas.ChargeFollowUpCreate(next_follow_up_on=today),
        organization_id=ORG,
    )
    with pytest.raises(AppError, match="exceeds"):
        create_payment(
            session,
            charge_rollback.id,
            schemas.PaymentCreate(amount=Decimal("151.00"), method="efectivo"),
            organization_id=ORG,
        )
    session.rollback()
    assert session.scalar(
        select(Payment).where(Payment.charge_id == charge_rollback.id)
    ) is None
    unchanged_follow_up = session.get(ChargeFollowUp, follow_up_rollback.id)
    assert unchanged_follow_up.state == "open"


def test_follow_up_tenancy_and_past_date(session):
    ids_a = _seed_chain(session, suffix="follow-up-tenant-a")
    charge_a = _make_charge(session, ids_a)
    charge_a_id = charge_a.id
    ids_b = _seed_chain(session, organization_id=create_organization(session, "FE3A tenant b").id, suffix="follow-up-tenant-b")
    charge_b = _make_charge(session, ids_b)
    charge_b_id = charge_b.id
    today = _today()
    with pytest.raises(AppError, match="past"):
        open_follow_up(
            session,
            charge_a_id,
            schemas.ChargeFollowUpCreate(next_follow_up_on=today - timedelta(days=1)),
            organization_id=ORG,
        )
    session.rollback()
    with pytest.raises(AppError) as foreign:
        open_follow_up(
            session,
            charge_b_id,
            schemas.ChargeFollowUpCreate(next_follow_up_on=today),
            organization_id=ORG,
        )
    assert foreign.value.code == ErrorCode.NOT_FOUND
    session.rollback()
    assert list_follow_ups(session, organization_id=ORG, active=True) == []


def test_concurrent_attendance_and_follow_up_guards(migrated_engine, session):
    ids = _seed_chain(session, suffix="concurrency")
    appointment = Appointment(
        organization_id=ORG,
        lead_id=ids["lead_id"],
        patient_id=ids["patient_id"],
        service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"],
        start_utc=datetime(2026, 9, 7, 15, 0, tzinfo=UTC),
        end_utc=datetime(2026, 9, 7, 15, 30, tzinfo=UTC),
        state="confirmed",
    )
    session.add(appointment)
    session.commit()
    charge = _make_charge(session, ids)
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def run_race(operation):
        barrier = threading.Barrier(2)
        results = []

        def worker():
            local = maker()
            try:
                barrier.wait(timeout=10)
                results.append(operation(local))
            except Exception as exc:  # noqa: BLE001 - race result is asserted below
                results.append(exc)
            finally:
                local.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        return results

    attendance_results = run_race(
        lambda local: create_visit(
            local,
            VisitCreate(patient_id=ids["patient_id"], appointment_id=appointment.id),
            organization_id=ORG,
        )
    )
    assert sum(isinstance(result, Visit) for result in attendance_results) == 1
    attendance_errors = [result for result in attendance_results if isinstance(result, AppError)]
    assert len(attendance_errors) == 1
    assert attendance_errors[0].code == ErrorCode.INVALID_INPUT
    assert session.scalar(
        select(Visit).where(Visit.organization_id == ORG, Visit.appointment_id == appointment.id)
    ) is not None

    follow_up_results = run_race(
        lambda local: open_follow_up(
            local,
            charge.id,
            schemas.ChargeFollowUpCreate(next_follow_up_on=_today()),
            organization_id=ORG,
        )
    )
    assert sum(isinstance(result, ChargeFollowUp) for result in follow_up_results) == 1
    follow_up_errors = [result for result in follow_up_results if isinstance(result, AppError)]
    assert len(follow_up_errors) == 1
    assert follow_up_errors[0].code == ErrorCode.INVALID_INPUT


def test_agent_boundary_has_no_new_economic_tools(session):
    agent_root = Path(__file__).parents[1] / "app" / "agent_tools"
    for source_file in agent_root.glob("*.py"):
        source = source_file.read_text()
        assert "app.economics" not in source
        for line in source.splitlines():
            if "app.clinical" in line:
                assert line.strip() == "from app.clinical.models import Patient"

    granted = session.execute(
        select(Role.code, Permission.code)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            Role.code.like("integration-%"),
            Permission.code.in_(
                ("payments.manage", "follow_ups.read", "follow_ups.create", "follow_ups.manage")
            ),
        )
    ).all()
    assert granted == []
