"""Phase 4 pilot booking: persisted proposal, explicit confirmation and replay."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.catalog.models import Service
from app.db import get_db
from app.messaging.models import ChannelAccount, ContactIdentity, Conversation
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import Appointment, AppointmentProposal, AvailabilityRule
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from conftest import AUTH_HEADERS


@pytest.fixture
def client(migrated_engine):
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
    return TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)


def _seed_booking_conversation(session, *, suffix: str, phone: str):
    service = Service(
        organization_id=ORG,
        name=f"Limpieza {suffix}",
        duration_minutes=60,
        is_active=True,
    )
    location = Location(
        organization_id=ORG,
        name=f"Sede {suffix}",
        timezone="America/Lima",
        is_active=True,
    )
    practitioner = Practitioner(display_name=f"Dra. {suffix}", is_active=True)
    channel = ChannelAccount(
        organization_id=ORG,
        provider="whatsapp",
        external_account_id=f"wa-{suffix}",
        phone_number_id=f"phone-{suffix}",
        display_name=f"WhatsApp {suffix}",
        is_active=True,
    )
    session.add_all([service, location, practitioner, channel])
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=ORG,
            practitioner_id=practitioner.id,
            is_active=True,
        )
    )
    session.flush()
    session.add_all(
        [
            PractitionerCapability(
                organization_id=ORG,
                practitioner_id=practitioner.id,
                service_id=service.id,
                location_id=location.id,
                is_active=True,
            ),
            AvailabilityRule(
                organization_id=ORG,
                practitioner_id=practitioner.id,
                location_id=location.id,
                day_of_week=0,
                start_local=time(9),
                end_local=time(12),
            ),
        ]
    )
    contact = ContactIdentity(
        organization_id=ORG,
        channel_account_id=channel.id,
        external_contact_id=f"contact-{suffix}",
        normalized_phone_e164=phone,
        consent_status="opted_in",
    )
    session.add(contact)
    session.flush()
    conversation = Conversation(
        organization_id=ORG,
        channel_account_id=channel.id,
        contact_identity_id=contact.id,
        status="open",
        last_message_at=datetime(2026, 8, 23, 15, tzinfo=UTC),
    )
    session.add(conversation)
    session.commit()
    return {
        "service": service,
        "location": location,
        "practitioner": practitioner,
        "contact": contact,
        "conversation": conversation,
    }


def _mutation_call(
    client,
    *,
    conversation_id: int,
    tool_name: str,
    arguments: dict,
    idempotency_key: str | None = None,
):
    key = idempotency_key or str(uuid4())
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    return client.post(
        "/agent-tools/call",
        headers={
            "Idempotency-Key": key,
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        },
        json={
            "tool_version": "1.1",
            "tool_name": tool_name,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "idempotency_key": key,
            "arguments": arguments,
        },
    )


def _propose(client, seeded, *, key: str | None = None):
    return _mutation_call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="propose_appointment",
        idempotency_key=key,
        arguments={
            "full_name": "Paciente Piloto",
            "service_id": seeded["service"].id,
            "location_id": seeded["location"].id,
            "practitioner_id": seeded["practitioner"].id,
            "start": "2026-08-24T09:00:00-05:00",
        },
    )


def test_proposal_persists_confirmation_state_without_booking(client, session):
    seeded = _seed_booking_conversation(
        session, suffix="proposal", phone="+51999110001"
    )

    response = _propose(client, seeded)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool_version"] == "1.1"
    assert body["status"] == "success"
    proposal = body["data"]["proposal"]
    assert proposal["status"] == "pending"
    assert proposal["start"] == "2026-08-24T14:00:00Z"
    assert proposal["end"] == "2026-08-24T15:00:00Z"
    assert proposal["confirmation_token"]

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    stored = session.get(AppointmentProposal, proposal["id"])
    assert stored is not None
    assert stored.status == "pending"
    contact = session.get(ContactIdentity, seeded["contact"].id)
    assert contact.lead_id is not None


def test_explicit_confirmation_books_once_and_returns_calendar_payload(client, session):
    seeded = _seed_booking_conversation(
        session, suffix="confirm", phone="+51999110002"
    )
    proposal = _propose(client, seeded).json()["data"]["proposal"]
    confirmation_key = str(uuid4())
    arguments = {
        "proposal_id": proposal["id"],
        "confirmation_token": proposal["confirmation_token"],
    }

    first = _mutation_call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="confirm_appointment",
        arguments=arguments,
        idempotency_key=confirmation_key,
    )
    replay = _mutation_call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="confirm_appointment",
        arguments=arguments,
        idempotency_key=confirmation_key,
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    first_data = first.json()["data"]
    replay_data = replay.json()["data"]
    assert first_data["appointment"]["state"] == "confirmed"
    assert replay_data["appointment"] == first_data["appointment"]
    assert first_data["replayed"] is False
    assert replay_data["replayed"] is True
    assert first_data["calendar_event"] == {
        "summary": "Cita dental - Limpieza confirm",
        "description": f"OdontoFlow cita #{first_data['appointment']['id']}",
        "start": "2026-08-24T09:00:00-05:00",
        "end": "2026-08-24T10:00:00-05:00",
        "timezone": "America/Lima",
    }

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1
    stored = session.get(AppointmentProposal, proposal["id"])
    assert stored.status == "confirmed"
    assert stored.appointment_id == first_data["appointment"]["id"]


def test_confirmation_is_bound_to_the_original_conversation(client, session):
    owner = _seed_booking_conversation(session, suffix="owner", phone="+51999110003")
    other = _seed_booking_conversation(session, suffix="other", phone="+51999110004")
    proposal = _propose(client, owner).json()["data"]["proposal"]

    denied = _mutation_call(
        client,
        conversation_id=other["conversation"].id,
        tool_name="confirm_appointment",
        arguments={
            "proposal_id": proposal["id"],
            "confirmation_token": proposal["confirmation_token"],
        },
    )

    assert denied.status_code == 200
    assert denied.json()["status"] == "error"
    assert denied.json()["error"]["code"] == "NOT_FOUND"
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0


def test_expired_proposal_cannot_be_confirmed(client, session):
    seeded = _seed_booking_conversation(
        session, suffix="expired", phone="+51999110005"
    )
    proposal = _propose(client, seeded).json()["data"]["proposal"]
    stored = session.get(AppointmentProposal, proposal["id"])
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    response = _mutation_call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="confirm_appointment",
        arguments={
            "proposal_id": proposal["id"],
            "confirmation_token": proposal["confirmation_token"],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0

