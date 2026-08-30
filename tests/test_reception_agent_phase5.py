"""Phase 5 — complete, contact-bound receptionist behavior."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.catalog.models import Promotion, Service
from app.clinical.models import Patient
from app.db import get_db
from app.messaging.models import (
    ChannelAccount,
    ContactIdentity,
    Conversation,
    ReceptionHandoff,
)
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import (
    Appointment,
    AppointmentRescheduleProposal,
    AvailabilityRule,
)
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


def _seed_reception(session, *, suffix: str, phone: str):
    service = Service(
        organization_id=ORG,
        name=f"Limpieza {suffix}",
        duration_minutes=60,
        public_description="Limpieza y profilaxis dental.",
        base_price="120.00",
        currency="PEN",
        booking_mode="automatic",
        is_active=True,
    )
    location = Location(
        organization_id=ORG,
        name=f"Sede {suffix}",
        timezone="America/Lima",
        address="Av. Prueba 123, Lima",
        public_phone="+5115550101",
        opening_hours={"monday": [{"open": "09:00", "close": "13:00"}]},
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
                end_local=time(13),
            ),
            Promotion(
                organization_id=ORG,
                code=f"PROMO-{suffix}",
                name="Sonrisa Smart",
                description="Consulta, evaluación y limpieza para pacientes nuevos.",
                promotional_price="99.00",
                currency="PEN",
                service_id=service.id,
                valid_from=date(2026, 8, 1),
                valid_until=date(2026, 8, 31),
                new_patients_only=True,
                priority=10,
                is_active=True,
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
        last_message_at=datetime(2026, 8, 25, 15, tzinfo=UTC),
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


READ_TOOLS = {
    "get_reception_context",
    "get_contact_profile",
}


def _call(client, *, conversation_id: int, tool_name: str, arguments: dict, key=None):
    mutation = tool_name not in READ_TOOLS
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    idem = (key or str(uuid4())) if mutation else None
    return client.post(
        "/agent-tools/call",
        headers={
            "Idempotency-Key": key or str(uuid4()),
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        },
        json={
            "tool_version": "1.1" if mutation else "1.0",
            "tool_name": tool_name,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "idempotency_key": idem,
            "arguments": arguments,
        },
    )


def test_reception_context_exposes_only_current_public_business_information(client, session):
    seeded = _seed_reception(session, suffix="context", phone="+51999120001")

    response = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="get_reception_context",
        arguments={"as_of": "2026-08-25"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["organization"]["name"]
    assert data["services"] == [
        {
            "id": seeded["service"].id,
            "name": "Limpieza context",
            "description": "Limpieza y profilaxis dental.",
            "duration_minutes": 60,
            "base_price": "120.00",
            "currency": "PEN",
            "booking_mode": "automatic",
        }
    ]
    assert data["locations"][0]["address"] == "Av. Prueba 123, Lima"
    assert data["locations"][0]["opening_hours"]["monday"][0]["open"] == "09:00"
    assert data["promotions"][0]["promotional_price"] == "99.00"
    assert "organization_id" not in str(data)


def test_register_contact_profile_is_idempotent_and_contact_bound(client, session):
    seeded = _seed_reception(session, suffix="profile", phone="+51999120002")
    key = str(uuid4())
    arguments = {
        "full_name": "Ana Pérez",
        "dni": "12345678",
        "birth_date": "1990-05-10",
    }

    first = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="register_contact_profile",
        arguments=arguments,
        key=key,
    )
    replay = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="register_contact_profile",
        arguments=arguments,
        key=key,
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["profile"] == replay.json()["data"]["profile"]
    assert first.json()["data"]["replayed"] is False
    assert replay.json()["data"]["replayed"] is True
    session.expire_all()
    contact = session.get(ContactIdentity, seeded["contact"].id)
    patient = session.get(Patient, contact.patient_id)
    assert patient.full_name == "Ana Pérez"
    assert patient.phone == "+51999120002"
    assert session.scalar(select(func.count()).select_from(Patient)) == 1


def test_contact_can_cancel_only_own_confirmed_appointment(client, session):
    own = _seed_reception(session, suffix="cancel-own", phone="+51999120003")
    other = _seed_reception(session, suffix="cancel-other", phone="+51999120004")
    own_profile = _call(
        client,
        conversation_id=own["conversation"].id,
        tool_name="register_contact_profile",
        arguments={"full_name": "Paciente Propio"},
    ).json()["data"]["profile"]
    other_profile = _call(
        client,
        conversation_id=other["conversation"].id,
        tool_name="register_contact_profile",
        arguments={"full_name": "Paciente Ajeno"},
    ).json()["data"]["profile"]
    own_appointment = Appointment(
        organization_id=ORG,
        lead_id=own_profile["lead_id"],
        patient_id=own_profile["patient_id"],
        service_id=own["service"].id,
        practitioner_id=own["practitioner"].id,
        location_id=own["location"].id,
        start_utc=datetime(2026, 8, 24, 14, tzinfo=UTC),
        end_utc=datetime(2026, 8, 24, 15, tzinfo=UTC),
        state="confirmed",
    )
    other_appointment = Appointment(
        organization_id=ORG,
        lead_id=other_profile["lead_id"],
        patient_id=other_profile["patient_id"],
        service_id=other["service"].id,
        practitioner_id=other["practitioner"].id,
        location_id=other["location"].id,
        start_utc=datetime(2026, 8, 24, 16, tzinfo=UTC),
        end_utc=datetime(2026, 8, 24, 17, tzinfo=UTC),
        state="confirmed",
    )
    session.add_all([own_appointment, other_appointment])
    session.commit()

    denied = _call(
        client,
        conversation_id=own["conversation"].id,
        tool_name="cancel_appointment",
        arguments={
            "appointment_id": other_appointment.id,
            "confirmation": "CONFIRMO_CANCELACION",
        },
    )
    accepted = _call(
        client,
        conversation_id=own["conversation"].id,
        tool_name="cancel_appointment",
        arguments={
            "appointment_id": own_appointment.id,
            "confirmation": "CONFIRMO_CANCELACION",
        },
    )

    assert denied.json()["error"]["code"] == "NOT_FOUND"
    assert accepted.json()["data"]["appointment"]["state"] == "cancelled"
    assert accepted.json()["data"]["calendar_action"] == {
        "action": "delete",
        "appointment_id": own_appointment.id,
    }


def test_reschedule_requires_proposal_then_explicit_confirmation(client, session):
    seeded = _seed_reception(session, suffix="move", phone="+51999120005")
    profile = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="register_contact_profile",
        arguments={"full_name": "Paciente Cambio"},
    ).json()["data"]["profile"]
    appointment = Appointment(
        organization_id=ORG,
        lead_id=profile["lead_id"],
        patient_id=profile["patient_id"],
        service_id=seeded["service"].id,
        practitioner_id=seeded["practitioner"].id,
        location_id=seeded["location"].id,
        start_utc=datetime(2026, 8, 24, 14, tzinfo=UTC),
        end_utc=datetime(2026, 8, 24, 15, tzinfo=UTC),
        state="confirmed",
    )
    session.add(appointment)
    session.commit()

    proposed = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="propose_reschedule",
        arguments={
            "appointment_id": appointment.id,
            "new_start": "2026-08-24T11:00:00-05:00",
        },
    ).json()["data"]["proposal"]
    session.expire_all()
    assert session.get(Appointment, appointment.id).start_utc == datetime(
        2026, 8, 24, 14, tzinfo=UTC
    )

    confirmed = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="confirm_reschedule",
        arguments={
            "proposal_id": proposed["id"],
            "confirmation_token": proposed["confirmation_token"],
        },
    )

    assert confirmed.json()["data"]["appointment"]["start"] == "2026-08-24T16:00:00Z"
    assert confirmed.json()["data"]["calendar_action"]["action"] == "update"
    session.expire_all()
    assert session.get(AppointmentRescheduleProposal, proposed["id"]).status == "confirmed"


def test_handoff_pauses_automation_and_persists_one_actionable_request(client, session):
    seeded = _seed_reception(session, suffix="handoff", phone="+51999120006")
    key = str(uuid4())
    arguments = {
        "reason_code": "urgent_symptoms",
        "reason_summary": "Dolor intenso e hinchazón; requiere evaluación humana.",
    }

    first = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="request_human_handoff",
        arguments=arguments,
        key=key,
    )
    replay = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="request_human_handoff",
        arguments=arguments,
        key=key,
    )

    assert first.json()["data"]["handoff"]["status"] == "pending"
    assert replay.json()["data"]["replayed"] is True
    session.expire_all()
    assert session.get(Conversation, seeded["conversation"].id).status == "human_handoff"
    assert session.scalar(select(func.count()).select_from(ReceptionHandoff)) == 1


def test_resume_automation_resolves_handoff_and_reopens_conversation(client, session):
    seeded = _seed_reception(session, suffix="resume", phone="+51999120016")
    _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="request_human_handoff",
        arguments={
            "reason_code": "requested_by_contact",
            "reason_summary": "El paciente solicitó hablar con recepción humana.",
        },
    )

    response = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="resume_automation",
        arguments={},
    )

    assert response.status_code == 200
    assert response.json()["data"]["automation"]["status"] == "open"
    session.expire_all()
    assert session.get(Conversation, seeded["conversation"].id).status == "open"
    handoff = session.scalar(
        select(ReceptionHandoff).where(
            ReceptionHandoff.conversation_id == seeded["conversation"].id
        )
    )
    assert handoff.status == "resolved"


@pytest.mark.parametrize("booking_mode", ["evaluation_first", "human_only"])
def test_reception_cannot_book_services_that_require_a_safer_next_step(
    client, session, booking_mode
):
    seeded = _seed_reception(
        session, suffix=f"policy-{booking_mode}", phone="+51999120007"
    )
    seeded["service"].booking_mode = booking_mode
    session.commit()

    response = _call(
        client,
        conversation_id=seeded["conversation"].id,
        tool_name="propose_appointment",
        arguments={
            "full_name": "Paciente Seguro",
            "service_id": seeded["service"].id,
            "location_id": seeded["location"].id,
            "practitioner_id": seeded["practitioner"].id,
            "start": "2026-08-24T09:00:00-05:00",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert booking_mode in response.json()["error"]["details"]["booking_mode"]

