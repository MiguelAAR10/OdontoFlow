"""SELF — patient self-booking through the frontend BFF (``POST /public/bookings``).

Spec: ``docs/superpowers/specs/2026-10-01-erp-self.md``. Real PostgreSQL, one
pytest process. The BFF holds an ``integration`` credential with the
``patient-booking`` profile; the patient is the confirming party (L3).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from test_agent_proposals import _credential, _other_org

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.context import default_context
from app.db import get_db
from app.events.models import DomainEvent
from app.messaging.models import ChannelAccount, ContactIdentity
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock
from app.scheduling.service import book_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import PROFILE_PERMISSIONS, profile_matches_type

LIMA = ZoneInfo("America/Lima")
UTC = timezone.utc


@pytest.fixture
def client(migrated_engine):
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    app = create_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False)


# --- helpers ------------------------------------------------------------------


def _day() -> date:
    """Two local days ahead: never "in the past", never the D-1 sweep's day."""
    return (datetime.now(LIMA) + timedelta(days=2)).date()


def _at(hour: int, minute: int = 0, day: date | None = None) -> datetime:
    d = day or _day()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=LIMA)


def _iso(value: datetime) -> str:
    return value.isoformat()


_N = iter(range(1, 100_000))


def _practitioner(session, ids, *, capable=True):
    org = ids["organization_id"]
    practitioner = Practitioner(display_name=f"Dr. {next(_N)}", is_active=True)
    session.add(practitioner)
    session.flush()
    session.add(
        PractitionerMembership(organization_id=org, practitioner_id=practitioner.id, is_active=True)
    )
    session.flush()
    if capable:
        session.add(
            PractitionerCapability(
                organization_id=org,
                practitioner_id=practitioner.id,
                service_id=ids["service_id"],
                location_id=ids["location_id"],
                is_active=True,
            )
        )
    for dow in range(7):
        session.add(
            AvailabilityRule(
                organization_id=org,
                practitioner_id=practitioner.id,
                location_id=ids["location_id"],
                day_of_week=dow,
                start_local=time(8),
                end_local=time(20),
            )
        )
    session.commit()
    return practitioner.id


def _seed(session, organization_id=ORG) -> dict:
    """Service (30 min) + Lima location + one capable practitioner, 08–20 every day."""
    n = next(_N)
    service = Service(
        organization_id=organization_id,
        name=f"Limpieza {n}",
        duration_minutes=30,
        base_price=Decimal("150.00"),
        currency="PEN",
        is_active=True,
    )
    location = Location(
        organization_id=organization_id, name=f"Sede {n}", timezone="America/Lima", is_active=True
    )
    session.add_all([service, location])
    session.commit()
    ids = {
        "organization_id": organization_id,
        "service_id": service.id,
        "location_id": location.id,
    }
    ids["practitioner_id"] = _practitioner(session, ids)
    return ids


def _bff(session, organization_id=ORG):
    return _credential(
        session,
        name=f"frontend-bff-{organization_id}",
        principal_type="integration",
        profile="patient-booking",
        organization_id=organization_id,
    )


def _body(ids, *, start=None, phone="+51987000001", name="  Ana Torres  ", practitioner=True, **extra):
    body = {
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "start": _iso(start or _at(9)),
        "full_name": name,
        "phone": phone,
    }
    if practitioner:
        body["practitioner_id"] = ids["practitioner_id"]
    body.update(extra)
    return body


def _book(client, headers, body, *, key=None, send_key=True):
    request_headers = dict(headers)
    if send_key:
        request_headers["Idempotency-Key"] = key or str(uuid4())
    return client.post("/public/bookings", json=body, headers=request_headers)


def _code(response) -> str:
    return response.json()["error"]["code"]


def _count(session, model, *where) -> int:
    session.expire_all()
    value = session.scalar(select(func.count()).select_from(model).where(*where))
    session.rollback()
    return value


def _identity(session, organization_id, phone, *, lead_id=None):
    n = next(_N)
    channel = ChannelAccount(
        organization_id=organization_id,
        provider="whatsapp",
        external_account_id=f"wa-self-{n}",
        phone_number_id=f"phone-self-{n}",
        display_name=f"WA {n}",
        is_active=True,
    )
    session.add(channel)
    session.flush()
    session.add(
        ContactIdentity(
            organization_id=organization_id,
            channel_account_id=channel.id,
            external_contact_id=f"contact-self-{n}",
            normalized_phone_e164=phone,
            lead_id=lead_id,
            consent_status="opted_in",
        )
    )
    session.commit()


def _lead(session, organization_id=ORG, *, phone=None, email=None, name="Lead Previo") -> int:
    lead = Lead(
        organization_id=organization_id,
        full_name=name,
        contact_phone=phone,
        contact_email=email,
        acquisition_source="direct",
    )
    session.add(lead)
    session.commit()
    return lead.id


# --- 1. valid booking -------------------------------------------------------------


def test_a_valid_booking_is_confirmed_with_a_reference(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    response = _book(client, bff, _body(ids))
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["state"] == "confirmed"
    assert data["reference"] == f"OF-{data['appointment_id']}"
    start = datetime.fromisoformat(data["start_utc"])
    end = datetime.fromisoformat(data["end_utc"])
    assert start == _at(9) and end - start == timedelta(minutes=30)
    assert data["practitioner_id"] == ids["practitioner_id"]
    assert set(data) == {
        "reference", "appointment_id", "state", "service_id", "location_id",
        "practitioner_id", "start_utc", "end_utc",
    }

    appointment = session.get(Appointment, data["appointment_id"])
    lead = session.get(Lead, appointment.lead_id)
    assert lead.full_name == "Ana Torres"
    assert lead.contact_phone == "+51987000001"
    assert lead.acquisition_source == "direct"
    assert lead.service_need_id == ids["service_id"]
    session.rollback()
    assert _count(session, Lead) == 1
    assert _count(
        session, AuditEvent,
        AuditEvent.action == "appointment.created",
        AuditEvent.entity_id == str(data["appointment_id"]),
    ) == 1
    session.expire_all()
    events = session.scalars(
        select(DomainEvent).where(DomainEvent.event_type == "appointment.booked_by_patient")
    ).all()
    assert len(events) == 1
    payload = json.dumps(events[0].payload)
    assert "+51987000001" not in payload and "Ana" not in payload
    assert events[0].payload["appointment_id"] == data["appointment_id"]
    session.rollback()


# --- 2. idempotency ---------------------------------------------------------------


def test_same_key_replays_and_a_different_body_is_rejected(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    key = str(uuid4())
    first = _book(client, bff, _body(ids), key=key)
    second = _book(client, bff, _body(ids), key=key)
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json()
    assert _count(session, Appointment) == 1

    reused = _book(client, bff, _body(ids, start=_at(10)), key=key)
    assert reused.status_code == 409 and _code(reused) == "IDEMPOTENCY_KEY_REUSED"
    missing = _book(client, bff, _body(ids, start=_at(11)), send_key=False)
    assert missing.status_code == 422 and _code(missing) == "INVALID_INPUT"
    assert _count(session, Appointment) == 1


# --- 3. slot classification -------------------------------------------------------


def test_slot_classification(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    assert _book(client, bff, _body(ids, phone="+51987000010")).status_code == 201

    taken = _book(client, bff, _body(ids, phone="+51987000011"))
    assert taken.status_code == 409 and _code(taken) == "SLOT_BLOCKED"

    session.add(
        ScheduleBlock(
            organization_id=ORG,
            practitioner_id=ids["practitioner_id"],
            location_id=ids["location_id"],
            start_utc=_at(12).astimezone(UTC),
            end_utc=_at(13).astimezone(UTC),
        )
    )
    session.commit()
    yesterday = (datetime.now(LIMA) - timedelta(days=1)).date()
    for start in (_at(10, 7), _at(21), _at(12), _at(10, day=yesterday)):
        response = _book(client, bff, _body(ids, start=start, phone="+51987000012"))
        assert response.status_code == 422, (start, response.text)
        assert _code(response) == "INVALID_INPUT"
    assert _count(session, Appointment) == 1


def test_omitted_practitioner_picks_the_first_free_capable_one(client, session):
    ids = _seed(session)
    second = _practitioner(session, ids)
    _bff_id, bff = _bff(session)
    a = _book(client, bff, _body(ids, phone="+51987000020", practitioner=False))
    b = _book(client, bff, _body(ids, phone="+51987000021", practitioner=False))
    assert a.status_code == 201 and b.status_code == 201, b.text
    assert a.json()["practitioner_id"] == ids["practitioner_id"]
    assert b.json()["practitioner_id"] == second
    full = _book(client, bff, _body(ids, phone="+51987000022", practitioner=False))
    assert full.status_code == 409 and _code(full) == "SLOT_BLOCKED"


def test_capability_is_checked_before_the_slot(client, session):
    ids = _seed(session)
    incapable = _practitioner(session, ids, capable=False)
    _bff_id, bff = _bff(session)
    response = _book(client, bff, _body(ids, practitioner_id=incapable))
    assert response.status_code == 409 and _code(response) == "CAPABILITY_MISSING"

    other = Service(
        organization_id=ORG, name="Ortodoncia sin doctores", duration_minutes=30,
        base_price=Decimal("90.00"), currency="PEN", is_active=True,
    )
    session.add(other)
    session.commit()
    nobody = _book(
        client, bff, _body({**ids, "service_id": other.id}, practitioner=False)
    )
    assert nobody.status_code == 409 and _code(nobody) == "CAPABILITY_MISSING"
    assert _count(session, Appointment) == 0


def test_only_an_integration_principal_may_book_as_the_patient(client, session):
    ids = _seed(session)
    assert profile_matches_type("patient-booking", "integration")
    assert not profile_matches_type("patient-booking", "agent")
    assert not profile_matches_type("patient-booking", "human")
    _agent_id, agent = _credential(
        session, name="agente-reserva", principal_type="agent", profile="patient-booking"
    )
    response = _book(client, agent, _body(ids))
    assert response.status_code == 403 and _code(response) == "PERMISSION_DENIED"
    assert _count(session, Appointment) == 0 and _count(session, Lead) == 0


# --- 4. lead reuse and schema -----------------------------------------------------


def test_lead_reuse_by_identity_then_by_lead_phone(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    identity_lead = _lead(session, phone="+51900000001", name="Por Identidad")
    _identity(session, ORG, "+51987000030", lead_id=identity_lead)
    phone_lead = _lead(session, phone="+51987000031", name="Por Telefono")

    a = _book(client, bff, _body(ids, phone="+51987000030"))
    b = _book(client, bff, _body(ids, phone="+51987000031", start=_at(10)))
    c = _book(client, bff, _body(ids, phone="+51987000032", start=_at(11)))
    assert a.status_code == b.status_code == c.status_code == 201
    assert session.get(Appointment, a.json()["appointment_id"]).lead_id == identity_lead
    assert session.get(Appointment, b.json()["appointment_id"]).lead_id == phone_lead
    new_lead = session.get(Appointment, c.json()["appointment_id"]).lead_id
    session.rollback()
    assert new_lead not in (identity_lead, phone_lead)
    assert _count(session, Lead) == 3
    assert _count(session, ContactIdentity) == 1  # no identity created at booking


@pytest.mark.parametrize(
    "extra",
    [{"end": "2030-01-01T10:00:00Z"}, {"state": "confirmed"}, {"duration_minutes": 5},
     {"lead_id": 1}],
)
def test_extra_fields_are_rejected(client, session, extra):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    response = _book(client, bff, _body(ids, **extra))
    assert response.status_code == 422 and _code(response) == "INVALID_INPUT"


def test_schema_rejects_non_e164_phone_and_naive_start(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    assert _book(client, bff, _body(ids, phone="987000001")).status_code == 422
    naive = _body(ids)
    naive["start"] = _at(9).replace(tzinfo=None).isoformat()
    assert _book(client, bff, naive).status_code == 422
    assert _book(client, bff, _body(ids, name="   ")).status_code == 422


# --- 5. rate limit ----------------------------------------------------------------


def test_rate_limit_per_phone(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    keys = [str(uuid4()) for _ in range(3)]
    for i, key in enumerate(keys):
        ok = _book(client, bff, _body(ids, start=_at(9 + i)), key=key)
        assert ok.status_code == 201, ok.text
    limited = _book(client, bff, _body(ids, start=_at(13)))
    assert limited.status_code == 429, limited.text
    assert _code(limited) == "PUBLIC_BOOKING_RATE_LIMITED"
    assert limited.json()["error"]["details"] == {"limit": 3, "window_hours": 24}
    assert _count(session, Appointment) == 3

    other = _book(client, bff, _body(ids, start=_at(13), phone="+51987000099"))
    assert other.status_code == 201
    replay = _book(client, bff, _body(ids, start=_at(9)), key=keys[0])
    assert replay.status_code == 201 and replay.headers.get("Idempotent-Replay") == "true"
    assert _count(session, Appointment) == 4


def test_rate_limit_counts_a_lead_matched_only_through_its_contact_identity(client, session):
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    email_only = _lead(session, email="solo@correo.pe", name="Solo Correo")
    _identity(session, ORG, "+51987000040", lead_id=email_only)
    for hour in (9, 10, 11):
        book_appointment(
            session,
            ctx=default_context(ORG),
            lead_id=email_only,
            service_id=ids["service_id"],
            location_id=ids["location_id"],
            practitioner_id=ids["practitioner_id"],
            start=_at(hour),
        )
    limited = _book(client, bff, _body(ids, start=_at(13), phone="+51987000040"))
    assert limited.status_code == 429, limited.text
    assert _count(session, Appointment) == 3


# --- 6. tenant isolation ----------------------------------------------------------


def test_tenant_isolation(client, session):
    ids_a = _seed(session)
    a_lead = _lead(session, phone="+51987000050", name="Paciente A")
    org_b = _other_org(session, name="Clinica SELF B")
    ids_b = _seed(session, organization_id=org_b)
    _bff_b, bff_b = _bff(session, organization_id=org_b)

    for body in (
        _body(ids_a, phone="+51987000051"),
        _body({**ids_b, "service_id": ids_a["service_id"]}, phone="+51987000051"),
        _body({**ids_b, "location_id": ids_a["location_id"]}, phone="+51987000051"),
        _body({**ids_b, "practitioner_id": ids_a["practitioner_id"]}, phone="+51987000051"),
    ):
        response = _book(client, bff_b, body)
        assert response.status_code == 404, (body, response.text)
        assert _code(response) == "NOT_FOUND"
    assert _count(session, Appointment) == 0

    ok = _book(client, bff_b, _body(ids_b, phone="+51987000050"))
    assert ok.status_code == 201, ok.text
    appointment = session.get(Appointment, ok.json()["appointment_id"])
    lead = session.get(Lead, appointment.lead_id)
    assert appointment.organization_id == org_b and lead.organization_id == org_b
    assert lead.id != a_lead
    session.rollback()


# --- 7. least privilege -----------------------------------------------------------


def test_patient_booking_credential_is_least_privilege(client, session):
    assert set(PROFILE_PERMISSIONS["patient-booking"]) == {
        "services.read", "locations.read", "practitioners.read", "availability.read",
        "appointments.create",
    }
    ids = _seed(session)
    _bff_id, bff = _bff(session)
    for path in ("/patients", "/charges", "/appointments", "/leads"):
        response = client.get(path, headers=bff)
        assert response.status_code == 403, (path, response.text)
    for agent_key in ("confirmaciones", "cobranza"):
        run = client.post(
            "/agent-runs", json={"agent_key": agent_key},
            headers={**bff, "Idempotency-Key": str(uuid4())},
        )
        assert run.status_code == 403, run.text

    assert client.get("/services", headers=bff).status_code == 200
    assert client.get("/locations", headers=bff).status_code == 200
    eligible = client.get(
        "/practitioners/eligible",
        params={"service_id": ids["service_id"], "location_id": ids["location_id"]},
        headers=bff,
    )
    assert eligible.status_code == 200, eligible.text
    slots = client.post(
        "/slots/query",
        json={
            "service_id": ids["service_id"],
            "location_id": ids["location_id"],
            "window_start": _iso(_at(8)),
            "window_end": _iso(_at(10)),
        },
        headers=bff,
    )
    assert slots.status_code == 200 and slots.json(), slots.text

    # Accepted residual (spec cut criteria): ``appointments.create`` also opens
    # ``POST /appointments``. A ``public_bookings.create`` code is the follow-up
    # that flips this assertion to 403.
    lead_id = _lead(session, phone="+51987000060")
    staff_route = client.post(
        "/appointments",
        json={
            "lead_id": lead_id,
            "service_id": ids["service_id"],
            "location_id": ids["location_id"],
            "practitioner_id": ids["practitioner_id"],
            "start": _iso(_at(15)),
        },
        headers={**bff, "Idempotency-Key": str(uuid4())},
    )
    assert staff_route.status_code == 201, staff_route.text
