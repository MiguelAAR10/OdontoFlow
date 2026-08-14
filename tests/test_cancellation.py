"""Task 9 — cancellation use case integration tests.

Every test exercises ``cancel_appointment`` against real PostgreSQL: the row
lock that serializes same-appointment mutation, the state transition that
preserves the interval, the single audit row written in the same transaction,
and the partial GiST predicate that releases the interval once the row is no
longer ``confirmed``.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import Appointment, AvailabilityRule
from app.scheduling.service import book_appointment, cancel_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)  # weekday 0, the anchor day used since Task 6


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed(session, *, duration_minutes=30, rule_window=(time(9, 0), time(13, 0))):
    service = Service(
        organization_id=ORG,
        name="Limpieza dental",
        duration_minutes=duration_minutes,
        is_active=True,
    )
    location = Location(
        organization_id=ORG, name="Sede Centro", timezone=LIMA, is_active=True
    )
    practitioner = Practitioner(display_name="Dra. Ana", is_active=True)
    lead = Lead(
        organization_id=ORG,
        full_name="Juan Pérez",
        contact_phone="+51999000111",
        acquisition_source="direct",
    )
    session.add_all([service, location, practitioner, lead])
    session.flush()
    # The global practitioner identity reaches this tenant's schedule only
    # through its membership row (PF0 PM2).
    session.add(
        PractitionerMembership(
            organization_id=ORG, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.flush()
    session.add(
        PractitionerCapability(
            organization_id=ORG,
            practitioner_id=practitioner.id,
            service_id=service.id,
            location_id=location.id,
            is_active=True,
        )
    )
    session.add(
        AvailabilityRule(
            organization_id=ORG,
            practitioner_id=practitioner.id,
            location_id=location.id,
            day_of_week=0,
            start_local=rule_window[0],
            end_local=rule_window[1],
        )
    )
    session.commit()
    return {
        "organization_id": ORG,
        "lead_id": lead.id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
    }


def book(session, ids, start=None, **overrides):
    payload = dict(ids)
    payload["start"] = start if start is not None else local(9)
    payload.update(overrides)
    return book_appointment(session, **payload)


def persisted_appointments(session):
    rows = session.execute(
        text("SELECT id, start_utc, end_utc, state FROM appointments ORDER BY id")
    ).all()
    session.rollback()
    return rows


def count_of(session, model):
    total = session.scalar(select(func.count()).select_from(model))
    session.rollback()
    return total


def audit_actions(session):
    actions = list(
        session.scalars(select(AuditEvent.action).order_by(AuditEvent.id))
    )
    session.rollback()
    return actions


# --- 1-2. the state transition ---------------------------------------------


def test_cancel_moves_a_confirmed_appointment_to_cancelled(session):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    cancelled = cancel_appointment(session, appointment_id)

    assert cancelled.id == appointment_id
    assert cancelled.state == "cancelled"
    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].id == appointment_id
    assert rows[0].state == "cancelled"


def test_cancellation_preserves_the_original_interval(session):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    cancelled = cancel_appointment(session, appointment_id)

    assert cancelled.start_utc == utc_of(9)
    assert cancelled.end_utc == utc_of(9, 30)
    rows = persisted_appointments(session)
    assert rows[0].start_utc == utc_of(9)
    assert rows[0].end_utc == utc_of(9, 30)


# --- 3. audit --------------------------------------------------------------


def test_cancellation_writes_exactly_one_cancelled_audit_event(session):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    cancel_appointment(
        session,
        appointment_id,
        actor_id="recepcion-01",
        actor_type="staff",
        correlation_id="corr-cancel-1",
    )

    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "appointment.cancelled")
            .order_by(AuditEvent.id)
        )
    )
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "appointment"
    assert event.entity_id == str(appointment_id)
    assert event.actor_id == "recepcion-01"
    assert event.actor_type == "staff"
    assert event.correlation_id == "corr-cancel-1"
    assert event.before_state["state"] == "confirmed"
    assert event.before_state["start_utc"] == utc_of(9).isoformat()
    assert event.before_state["end_utc"] == utc_of(9, 30).isoformat()
    assert event.after_state["state"] == "cancelled"
    assert event.after_state["start_utc"] == utc_of(9).isoformat()
    assert event.after_state["end_utc"] == utc_of(9, 30).isoformat()
    assert event.occurred_at is not None
    session.rollback()


def test_cancellation_audit_defaults_to_the_system_actor(session):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    cancel_appointment(session, appointment_id)

    event = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "appointment.cancelled")
    ).one()
    assert event.actor_id == "system"
    assert event.actor_type == "system"
    assert event.correlation_id is None
    session.rollback()


# --- 4. atomicity ----------------------------------------------------------


def test_failed_cancellation_writes_no_audit_and_leaves_the_state_unchanged(
    session, monkeypatch
):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    def boom(*args, **kwargs):
        raise RuntimeError("audit writer exploded")

    monkeypatch.setattr("app.scheduling.service.record_event", boom)

    with pytest.raises(RuntimeError):
        cancel_appointment(session, appointment_id)

    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].state == "confirmed"  # the mutation rolled back with the audit
    assert audit_actions(session) == ["appointment.created"]


def test_rejected_cancellation_leaves_neither_mutation_nor_audit(session):
    ids = seed(session)

    with pytest.raises(AppError):
        cancel_appointment(session, 999_999)

    assert count_of(session, Appointment) == 0
    assert count_of(session, AuditEvent) == 0


# --- 5. the interval is released -------------------------------------------


def test_cancelled_appointment_releases_the_interval_for_a_new_booking(session):
    ids = seed(session)
    first = book(session, ids, start=local(9))

    cancel_appointment(session, first.id)
    second = book(session, ids, start=local(9))

    assert second.id != first.id
    assert second.state == "confirmed"
    assert second.start_utc == utc_of(9)
    rows = persisted_appointments(session)
    assert len(rows) == 2
    assert [row.state for row in rows] == ["cancelled", "confirmed"]


# --- 6-7. missing appointment and double cancellation ----------------------


def test_cancel_missing_appointment_raises_not_found(session):
    seed(session)

    with pytest.raises(AppError) as exc:
        cancel_appointment(session, 999_999)

    assert exc.value.code is ErrorCode.NOT_FOUND
    assert exc.value.http_status == 404


def test_double_cancellation_raises_a_stable_conflict(session):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id
    cancel_appointment(session, appointment_id)

    with pytest.raises(AppError) as exc:
        cancel_appointment(session, appointment_id)

    assert exc.value.code is ErrorCode.ENTITY_INACTIVE
    assert exc.value.http_status == 409
    # Deterministic and repeatable: the same call keeps producing the same code.
    with pytest.raises(AppError) as again:
        cancel_appointment(session, appointment_id)
    assert again.value.code is ErrorCode.ENTITY_INACTIVE

    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].state == "cancelled"
    assert audit_actions(session) == ["appointment.created", "appointment.cancelled"]


# --- API -------------------------------------------------------------------


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
    return app


@pytest.fixture
def client(api_app):
    return TestClient(api_app, raise_server_exceptions=False)


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_cancel_endpoint_returns_200_with_the_typed_appointment(session, client):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    response = client.post(f"/appointments/{appointment_id}/cancel")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == appointment_id
    assert body["state"] == "cancelled"
    assert _dt(body["start_utc"]) == utc_of(9)
    assert _dt(body["end_utc"]) == utc_of(9, 30)
    assert body["lead_id"] == ids["lead_id"]
    assert body["service_id"] == ids["service_id"]
    assert body["practitioner_id"] == ids["practitioner_id"]
    assert body["location_id"] == ids["location_id"]


def test_cancel_endpoint_missing_appointment_returns_404_envelope(session, client):
    seed(session)

    response = client.post("/appointments/999999/cancel")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Appointment not found.",
            "details": {},
        }
    }


def test_cancel_endpoint_double_cancel_returns_409_envelope(session, client):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id
    assert client.post(f"/appointments/{appointment_id}/cancel").status_code == 200

    response = client.post(f"/appointments/{appointment_id}/cancel")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "ENTITY_INACTIVE"
    assert body["error"]["details"] == {}
    assert "Traceback" not in response.text


def test_cancel_endpoint_forbids_unknown_body_fields(session, client):
    ids = seed(session)
    appointment = book(session, ids)
    appointment_id = appointment.id

    response = client.post(
        f"/appointments/{appointment_id}/cancel", json={"state": "confirmed"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    rows = persisted_appointments(session)
    assert rows[0].state == "confirmed"
