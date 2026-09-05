"""Phase 3 — contact-safe, read-only agent tool gateway."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.messaging.models import ChannelAccount, ContactIdentity, Conversation
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import Appointment, AvailabilityRule
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


def _seed_conversation(session, *, suffix: str, phone: str):
    service = Service(
        organization_id=ORG,
        name=f"Limpieza {suffix}",
        duration_minutes=30,
        is_active=True,
    )
    location = Location(
        organization_id=ORG,
        name=f"Sede {suffix}",
        timezone="America/Lima",
        is_active=True,
    )
    practitioner = Practitioner(display_name=f"Dra. {suffix}", is_active=True)
    lead = Lead(
        organization_id=ORG,
        full_name=f"Contacto {suffix}",
        contact_phone=phone,
        acquisition_source="direct",
    )
    channel = ChannelAccount(
        organization_id=ORG,
        provider="whatsapp",
        external_account_id=f"wa-{suffix}",
        phone_number_id=f"phone-{suffix}",
        display_name=f"WhatsApp {suffix}",
        is_active=True,
    )
    session.add_all([service, location, practitioner, lead, channel])
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
        lead_id=lead.id,
        consent_status="opted_in",
    )
    session.add(contact)
    session.flush()
    conversation = Conversation(
        organization_id=ORG,
        channel_account_id=channel.id,
        contact_identity_id=contact.id,
        status="open",
        last_message_at=datetime(2026, 8, 20, 15, tzinfo=UTC),
    )
    session.add(conversation)
    session.flush()
    appointment = Appointment(
        organization_id=ORG,
        lead_id=lead.id,
        service_id=service.id,
        practitioner_id=practitioner.id,
        location_id=location.id,
        start_utc=datetime(2026, 8, 24, 14, tzinfo=UTC),
        end_utc=datetime(2026, 8, 24, 14, 30, tzinfo=UTC),
        state="confirmed",
    )
    session.add(appointment)
    session.commit()
    return {
        "service": service,
        "location": location,
        "practitioner": practitioner,
        "lead": lead,
        "conversation": conversation,
        "appointment": appointment,
    }


def _assert_tool_result(response, *, key: str):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool_version"] == "1.0"
    assert body["status"] == "success"
    assert body["error"] is None
    assert key in body["data"]
    assert body["request_id"] == response.headers["X-Request-Id"]
    assert body["correlation_id"] == response.headers["X-Correlation-Id"]
    assert isinstance(body["duration_ms"], int)
    return body["data"][key]


def _call(client, conversation_id: int, tool_name: str, arguments: dict):
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    response = client.post(
        "/agent-tools/call",
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        },
        json={
            "tool_version": "1.0",
            "tool_name": tool_name,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "idempotency_key": None,
            "arguments": arguments,
        },
    )
    UUID(response.headers["X-Request-Id"])
    UUID(response.headers["X-Correlation-Id"])
    return response


def test_catalog_tools_return_only_active_minimal_dtos(client, session):
    seeded = _seed_conversation(session, suffix="a", phone="+51999000111")
    session.add(
        Service(
            organization_id=ORG,
            name="Servicio desactivado",
            duration_minutes=45,
            is_active=False,
        )
    )
    session.commit()

    conversation_id = seeded["conversation"].id
    services = _assert_tool_result(
        _call(client, conversation_id, "list_services", {}), key="services"
    )
    locations = _assert_tool_result(
        _call(client, conversation_id, "list_locations", {}), key="locations"
    )
    practitioners = _assert_tool_result(
        _call(
            client,
            conversation_id,
            "list_eligible_practitioners",
            {
                "service_id": seeded["service"].id,
                "location_id": seeded["location"].id,
            },
        ),
        key="practitioners",
    )

    assert services == [
        {
            "id": seeded["service"].id,
            "name": "Limpieza a",
            "duration_minutes": 30,
        }
    ]
    assert locations == [
        {
            "id": seeded["location"].id,
            "name": "Sede a",
            "timezone": "America/Lima",
        }
    ]
    assert practitioners == [
        {"id": seeded["practitioner"].id, "display_name": "Dra. a"}
    ]
    assert "organization_id" not in str(services + locations + practitioners)


def test_slot_tool_is_bounded_and_returns_only_api_computed_slots(client, session):
    seeded = _seed_conversation(session, suffix="slots", phone="+51999000222")
    response = _call(
        client,
        seeded["conversation"].id,
        "query_available_slots",
        {
            "service_id": seeded["service"].id,
            "location_id": seeded["location"].id,
            "window_start": "2026-08-24T13:00:00Z",
            "window_end": "2026-08-24T18:00:00Z",
        },
    )
    slots = _assert_tool_result(response, key="slots")
    assert 1 <= len(slots) <= 100
    assert all(slot["practitioner_id"] == seeded["practitioner"].id for slot in slots)
    assert slots[0]["timezone"] == "America/Lima"
    assert slots[0]["start_local"] == "2026-08-24T09:30:00-05:00"
    assert slots[0]["end_local"] == "2026-08-24T10:00:00-05:00"
    assert slots[0]["date_local"] == "2026-08-24"
    assert slots[0]["weekday_local"] == "lunes"
    assert slots[0]["time_local"] == "09:30"

    too_wide = _call(
        client,
        seeded["conversation"].id,
        "query_available_slots",
        {
            "service_id": seeded["service"].id,
            "location_id": seeded["location"].id,
            "window_start": "2026-08-01T00:00:00Z",
            "window_end": "2026-09-01T00:00:00Z",
        },
    )
    assert too_wide.status_code == 200
    assert too_wide.json()["status"] == "error"
    assert too_wide.json()["error"]["code"] == "INVALID_INPUT"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "query_available_slots",
            {
                "service_id": 1,
                "location_id": 1,
                "window_start": "2026-08-24T13:00:00",
                "window_end": "2026-08-24T18:00:00Z",
            },
        ),
        (
            "query_available_slots",
            {
                "service_id": 1,
                "location_id": 1,
                "window_start": "2026-08-24T18:00:00Z",
                "window_end": "2026-08-24T13:00:00Z",
            },
        ),
        (
            "list_contact_appointments",
            {
                "from_date": "2026-08-25T00:00:00Z",
                "to_date": "2026-08-24T00:00:00Z",
            },
        ),
    ],
)
def test_tool_date_windows_reject_ambiguous_or_reversed_ranges(
    client, session, tool_name, arguments
):
    seeded = _seed_conversation(
        session,
        suffix=f"invalid-window-{tool_name}",
        phone="+51999000666",
    )

    response = _call(client, seeded["conversation"].id, tool_name, arguments)

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_tool_trace_headers_and_envelope_must_match(client, session):
    seeded = _seed_conversation(session, suffix="trace", phone="+51999000777")
    header_request_id = str(uuid4())
    response = client.post(
        "/agent-tools/call",
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Request-Id": header_request_id,
            "X-Correlation-Id": str(uuid4()),
        },
        json={
            "tool_version": "1.0",
            "tool_name": "list_services",
            "conversation_id": seeded["conversation"].id,
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "idempotency_key": None,
            "arguments": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert response.json()["request_id"] == header_request_id


def test_contact_appointment_tools_cannot_enumerate_another_contact(client, session):
    own = _seed_conversation(session, suffix="own", phone="+51999000333")
    other = _seed_conversation(session, suffix="other", phone="+51999000444")

    appointments = _assert_tool_result(
        _call(
            client,
            own["conversation"].id,
            "list_contact_appointments",
            {},
        ),
        key="appointments",
    )
    assert [row["id"] for row in appointments] == [own["appointment"].id]
    assert "lead_id" not in appointments[0]
    assert "lead_name" not in appointments[0]

    denied = _call(
        client,
        own["conversation"].id,
        "get_appointment",
        {"appointment_id": other["appointment"].id},
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "error"
    assert denied.json()["error"]["code"] == "NOT_FOUND"


def test_tool_call_audit_keeps_trace_and_excludes_prompt_content(client, session):
    correlation = "9d0e6ca9-8572-4526-85a4-7cf432fdb1ba"
    seeded = _seed_conversation(session, suffix="audit", phone="+51999000555")
    request_id = str(uuid4())
    response = client.post(
        "/agent-tools/call",
        headers={
            "Idempotency-Key": str(uuid4()),
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation,
        },
        json={
            "tool_version": "1.0",
            "tool_name": "list_services",
            "conversation_id": seeded["conversation"].id,
            "request_id": request_id,
            "correlation_id": correlation,
            "idempotency_key": None,
            "arguments": {},
        },
    )
    assert response.status_code == 200

    event = session.execute(
        text(
            "SELECT action, request_id, correlation_id, after_state "
            "FROM audit_events WHERE entity_type='agent_tool' "
            "ORDER BY id DESC LIMIT 1"
        )
    ).one()
    assert event.action == "agent_tool.called"
    assert event.request_id == response.headers["X-Request-Id"]
    assert event.correlation_id == correlation
    assert event.after_state["tool_name"] == "list_services"
    assert event.after_state["status"] == "success"
    assert isinstance(event.after_state["duration_ms"], int)
    serialized = str(event.after_state).lower()
    assert "prompt" not in serialized
    assert "authorization" not in serialized


def test_agent_tool_openapi_has_no_business_mutation_routes(client):
    spec = client.get("/openapi.json").json()
    paths = {path for path in spec["paths"] if path.startswith("/agent-tools")}
    assert paths == {"/agent-tools/call"}
    forbidden = ("/appointments", "/leads", "/products", "/payments")
    assert all(not any(path == prefix for prefix in forbidden) for path in paths)

