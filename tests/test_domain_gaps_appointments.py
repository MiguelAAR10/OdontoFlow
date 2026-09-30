"""B0.5 §1 — appointment outcomes ``completed`` / ``no_show`` (spec 2026-10-01)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.audit.models import AuditEvent
from app.clinical.schemas import PatientCreate, VisitCreate
from app.clinical.service import create_patient, create_visit
from app.context import default_context
from app.errors import AppError, ErrorCode
from app.iam.permissions import APPOINTMENTS_READ, APPOINTMENTS_RECORD_OUTCOME
from app.iam.service import IamErrorCode
from app.organization.service import create_organization
from app.scheduling.models import Appointment
from app.scheduling.service import cancel_appointment, complete_appointment, mark_no_show
from test_domain_gaps_helpers import (  # noqa: F401  (``api`` is a fixture)
    FUTURE_MONDAY,
    ORG,
    actor_ctx,
    api,
    book,
    domain_events,
    seed_booking,
)


def _state(session, appointment_id: int) -> str:
    session.expire_all()
    state = session.get(Appointment, appointment_id).state
    session.rollback()
    return state


def _audit_count(session, action: str, entity_id: int) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == action, AuditEvent.entity_id == str(entity_id))
    )
    session.rollback()
    return count


@pytest.mark.parametrize(
    ("path", "state", "event_type"),
    [
        ("complete", "completed", "appointment.completed"),
        ("no-show", "no_show", "appointment.no_show"),
    ],
)
def test_past_confirmed_appointment_records_outcome(api, session, path, state, event_type):
    ids = seed_booking(session)
    appointment_id = book(session, ids)

    response = api.post(
        f"/appointments/{appointment_id}/{path}",
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == state
    assert _state(session, appointment_id) == state
    assert _audit_count(session, event_type, appointment_id) == 1
    events = domain_events(session, event_type, str(appointment_id))
    assert len(events) == 1
    assert events[0].aggregate_type == "appointment"
    assert events[0].organization_id == ORG


def test_future_appointment_cannot_be_marked_no_show(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids, day=FUTURE_MONDAY)

    response = api.post(f"/appointments/{appointment_id}/no-show")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert _state(session, appointment_id) == "confirmed"
    assert domain_events(session, "appointment.no_show") == []


def test_future_appointment_cannot_be_completed(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids, day=FUTURE_MONDAY)

    response = api.post(f"/appointments/{appointment_id}/complete")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert _state(session, appointment_id) == "confirmed"


def test_cancelled_appointment_cannot_be_completed(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    cancel_appointment(session, appointment_id, ctx=default_context(ORG))

    response = api.post(f"/appointments/{appointment_id}/complete")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ENTITY_INACTIVE"
    assert _state(session, appointment_id) == "cancelled"


def test_no_show_cannot_become_completed(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    assert api.post(f"/appointments/{appointment_id}/no-show").status_code == 200

    response = api.post(f"/appointments/{appointment_id}/complete")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ENTITY_INACTIVE"
    assert _state(session, appointment_id) == "no_show"


def test_double_complete_with_same_key_replays_and_emits_once(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    key = str(uuid4())

    first = api.post(f"/appointments/{appointment_id}/complete", headers={"Idempotency-Key": key})
    replay = api.post(f"/appointments/{appointment_id}/complete", headers={"Idempotency-Key": key})
    other_key = api.post(
        f"/appointments/{appointment_id}/complete", headers={"Idempotency-Key": str(uuid4())}
    )

    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["state"] == "completed"
    assert other_key.status_code == 409
    assert other_key.json()["error"]["code"] == "ENTITY_INACTIVE"
    assert len(domain_events(session, "appointment.completed", str(appointment_id))) == 1
    assert _audit_count(session, "appointment.completed", appointment_id) == 1


def test_completed_appointment_releases_the_practitioner_interval(session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    complete_appointment(session, appointment_id, ctx=default_context(ORG))

    # The GiST only covers ``confirmed``: the same interval is bookable again.
    rebooked = book(session, ids)
    assert rebooked != appointment_id
    assert _state(session, rebooked) == "confirmed"


def test_other_organization_appointment_is_not_found(api, session):
    org_b = create_organization(session, "Clínica B0.5").id
    session.commit()
    ids_b = seed_booking(session, organization_id=org_b, suffix="b")
    foreign_id = book(session, ids_b)

    response = api.post(f"/appointments/{foreign_id}/complete")

    assert response.status_code == 404, response.text
    assert _state(session, foreign_id) == "confirmed"


def test_recording_an_outcome_requires_the_permission(session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    reader = actor_ctx(session, codes=(APPOINTMENTS_READ,))

    with pytest.raises(AppError) as raised:
        mark_no_show(session, appointment_id, ctx=reader)
    assert raised.value.code == IamErrorCode.PERMISSION_DENIED
    session.rollback()

    allowed = actor_ctx(session, codes=(APPOINTMENTS_RECORD_OUTCOME,))
    assert mark_no_show(session, appointment_id, ctx=allowed).state == "no_show"


def test_completed_appointment_can_no_longer_originate_a_visit(session):
    """Documents the approved order: create the visit while confirmed, then complete."""
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    ctx = default_context(ORG)
    patient = create_patient(session, PatientCreate(full_name="Paciente Visita", dni="71000099"), ctx=ctx)
    complete_appointment(session, appointment_id, ctx=ctx)

    with pytest.raises(AppError) as raised:
        create_visit(
            session, VisitCreate(patient_id=patient.id, appointment_id=appointment_id), ctx=ctx
        )
    assert raised.value.code is ErrorCode.ENTITY_INACTIVE


def test_visit_first_then_complete_is_the_supported_order(session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    ctx = default_context(ORG)
    patient = create_patient(session, PatientCreate(full_name="Paciente Orden", dni="71000098"), ctx=ctx)
    create_visit(session, VisitCreate(patient_id=patient.id, appointment_id=appointment_id), ctx=ctx)

    assert complete_appointment(session, appointment_id, ctx=ctx).state == "completed"


def test_cancel_and_reschedule_still_reject_outcome_states(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids)
    assert api.post(f"/appointments/{appointment_id}/complete").status_code == 200

    cancel = api.post(f"/appointments/{appointment_id}/cancel")

    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "ENTITY_INACTIVE"


def test_staff_cancellation_emits_a_domain_event(api, session):
    ids = seed_booking(session)
    appointment_id = book(session, ids, day=FUTURE_MONDAY)

    response = api.post(f"/appointments/{appointment_id}/cancel")

    assert response.status_code == 200, response.text
    events = domain_events(session, "appointment.cancelled", str(appointment_id))
    assert len(events) == 1
    assert events[0].aggregate_type == "appointment"


def test_contact_confirmed_cancellation_emits_a_domain_event(migrated_engine, session):
    """``app/agent_tools/reception.py`` cancellation path (authorized deviation)."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from conftest import AUTH_HEADERS
    from app import create_app
    from app.db import get_db
    from test_reception_agent_phase5 import _add_inbound_message, _call, _seed_reception

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
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)

    seeded = _seed_reception(session, suffix="b05-cancel", phone="+51999120777")
    profile = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="register_contact_profile",
        arguments={"full_name": "Paciente Cancela"},
    ).json()["data"]["profile"]
    appointment = Appointment(
        organization_id=ORG,
        lead_id=profile["lead_id"],
        patient_id=profile["patient_id"],
        service_id=seeded["service"].id,
        practitioner_id=seeded["practitioner"].id,
        location_id=seeded["location"].id,
        start_utc=datetime(2026, 8, 24, 14, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 24, 15, tzinfo=timezone.utc),
        state="confirmed",
    )
    session.add(appointment)
    session.commit()
    first = _add_inbound_message(session, seeded, suffix="b05-cancel-1")
    proposal = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="propose_cancellation",
        arguments={"appointment_id": appointment.id, "source_message_id": first.id},
    ).json()["data"]["proposal"]
    second = _add_inbound_message(session, seeded, suffix="b05-cancel-2")

    accepted = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="confirm_cancellation",
        arguments={
            "proposal_id": proposal["id"],
            "confirmation_token": proposal["confirmation_token"],
            "source_message_id": second.id,
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert _state(session, appointment.id) == "cancelled"
    assert len(domain_events(session, "appointment.cancelled", str(appointment.id))) == 1
