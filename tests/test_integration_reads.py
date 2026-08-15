"""Sprint reads — agenda/leads/locations integration reads against real PostgreSQL.

Proves the four read endpoints added for the Agenda vertical: tenant scoping
(cross-org rows invisible), the half-open date window, location/practitioner
filters, the joined display names, permission enforcement (org-wide and
location-scoped grants), and the full agenda journey (book → list → reload →
reschedule → cancel) over HTTP with idempotency keys.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.iam.permissions import (
    APPOINTMENTS_CREATE,
    APPOINTMENTS_READ,
    APPOINTMENTS_RESCHEDULE,
    APPOINTMENTS_CANCEL,
    LEADS_READ,
    LOCATIONS_READ,
)
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
)
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.service import create_organization
from app.scheduling.models import Appointment, AvailabilityRule
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
    session.add(
        AvailabilityRule(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            location_id=location.id,
            day_of_week=5,
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
    principal = create_principal(
        session, display_name="actor", principal_type="human"
    )
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
    session.rollback()
    return principal.id


def book(client, ids, *, start, key=None):
    payload = {
        "lead_id": ids["lead_id"],
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "practitioner_id": ids["practitioner_id"],
        "start": start.isoformat(),
    }
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/appointments", json=payload, headers=headers)


# --- agenda list -------------------------------------------------------------


def test_agenda_lists_only_own_organization_appointments(client, session):
    ids_a = seed_booking(session, name_suffix="a")
    org_b = create_organization(session, "Otra Clínica").id
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")

    book(client, ids_a, start=utc_of(9), key="key-a-1")
    book(client, ids_b, start=utc_of(9), key="key-b-1")

    response = client.get("/appointments")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1, rows
    assert rows[0]["lead_name"] == "Juan Pérez a"
    assert rows[0]["service_name"] == "Servicio a"
    assert rows[0]["practitioner_name"] == "Dra. Ana a"
    assert rows[0]["location_name"] == "Sede a"


def test_agenda_filters_by_half_open_date_window(client, session):
    ids = seed_booking(session)
    week_start = datetime(2026, 8, 10, tzinfo=UTC)
    week_end = datetime(2026, 8, 16, tzinfo=UTC)
    book(client, ids, start=utc_of(9, day=MONDAY), key="key-in-1")
    book(client, ids, start=utc_of(9, day=date(2026, 8, 15)), key="key-in-2")

    inside = client.get(
        "/appointments",
        params={"from_date": week_start.isoformat(), "to_date": week_end.isoformat()},
    )
    assert inside.status_code == 200
    assert len(inside.json()) == 2

    # Monday's instant is 14:00 UTC (09:00 Lima); the window [Mon, Tue) must
    # contain it and the Saturday appointment must fall outside.
    only_monday = client.get(
        "/appointments",
        params={
            "from_date": week_start.isoformat(),
            "to_date": datetime(2026, 8, 11, tzinfo=UTC).isoformat(),
        },
    )
    assert len(only_monday.json()) == 1
    assert only_monday.json()[0]["start_utc"].startswith("2026-08-10")


def test_agenda_filters_by_location_and_practitioner(client, session):
    ids = seed_booking(session, name_suffix="main")
    other_practitioner = Practitioner(display_name="Dr. Otro", is_active=True)
    session.add(other_practitioner)
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=ORG, practitioner_id=other_practitioner.id, is_active=True
        )
    )
    session.commit()

    book(client, ids, start=utc_of(9), key="key-l1")

    by_location = client.get("/appointments", params={"location_id": ids["location_id"]})
    assert len(by_location.json()) == 1
    by_other_location = client.get("/appointments", params={"location_id": 9999})
    assert len(by_other_location.json()) == 0
    by_practitioner = client.get(
        "/appointments", params={"practitioner_id": ids["practitioner_id"]}
    )
    assert len(by_practitioner.json()) == 1


def test_agenda_returns_cancelled_appointments_with_state(client, session):
    ids = seed_booking(session)
    booked = book(client, ids, start=utc_of(9), key="key-c1")
    appointment_id = booked.json()["id"]
    client.post(f"/appointments/{appointment_id}/cancel", headers={"Idempotency-Key": "key-c2"})

    rows = client.get("/appointments").json()
    assert len(rows) == 1
    assert rows[0]["state"] == "cancelled"


def test_appointment_detail_returns_names(client, session):
    ids = seed_booking(session)
    booked = book(client, ids, start=utc_of(9), key="key-d1")
    response = client.get(f"/appointments/{booked.json()['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["lead_name"] == "Juan Pérez 1"
    assert body["state"] == "confirmed"


def test_appointment_detail_not_found_cross_tenant(client, session):
    org_b = create_organization(session, "Otra Clínica").id
    ids_b = seed_booking(session, organization_id=org_b, name_suffix="b")
    # Seed org B's appointment directly: the HTTP client's default context
    # acts in the bootstrap org and must never see it.
    session.add(
        Appointment(
            organization_id=org_b,
            lead_id=ids_b["lead_id"],
            service_id=ids_b["service_id"],
            practitioner_id=ids_b["practitioner_id"],
            location_id=ids_b["location_id"],
            start_utc=utc_of(9),
            end_utc=utc_of(9, 30),
            state="confirmed",
        )
    )
    session.commit()
    appointment_id = session.scalar(text("SELECT id FROM appointments WHERE organization_id = :o"), {"o": org_b})
    session.rollback()

    response = client.get(f"/appointments/{appointment_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- leads / locations -------------------------------------------------------


def test_leads_list_is_scoped_and_searchable(client, session):
    seed_booking(session, name_suffix="1")
    org_b = create_organization(session, "Otra Clínica").id
    seed_booking(session, organization_id=org_b, name_suffix="2")

    all_rows = client.get("/leads")
    assert all_rows.status_code == 200
    names = [row["full_name"] for row in all_rows.json()]
    assert names == ["Juan Pérez 1"]
    assert all(row["commercial_status"] == "new" for row in all_rows.json())

    found = client.get("/leads", params={"search": "juan"})
    assert len(found.json()) == 1
    by_phone = client.get("/leads", params={"search": "9000001"})
    assert len(by_phone.json()) == 1
    missing = client.get("/leads", params={"search": "zzz"})
    assert len(missing.json()) == 0


def test_locations_list_is_scoped(client, session):
    seed_booking(session, name_suffix="main")
    org_b = create_organization(session, "Otra Clínica").id
    seed_booking(session, organization_id=org_b, name_suffix="b")

    response = client.get("/locations")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Sede main"
    assert rows[0]["timezone"] == LIMA


# --- permissions (service-level: HTTP identity is still the default context) -


def ctx_for(principal_id, organization_id=ORG):
    from app.iam.context import ExecutionContext

    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal_id,
        principal_type="human",
        request_id="req-reads",
        correlation_id="corr-reads",
    )


def test_agenda_reads_enforce_permissions(session):
    from app.commercial.service import list_leads
    from app.organization.service import list_locations
    from app.scheduling.service import get_appointment, list_appointments

    ids = seed_booking(session)
    # Seed one appointment through the service with the system context.
    from app.context import default_context
    from app.scheduling.service import book_appointment

    appointment = book_appointment(
        session,
        ctx=default_context(ORG),
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        location_id=ids["location_id"],
        practitioner_id=ids["practitioner_id"],
        start=utc_of(9),
    )

    no_perm = seed_actor(session, codes=())
    ctx = ctx_for(no_perm)

    for operation in (
        lambda: list_appointments(session, ctx=ctx),
        lambda: get_appointment(session, appointment.id, ctx=ctx),
        lambda: list_leads(session, ctx=ctx),
        lambda: list_locations(session, ctx=ctx),
    ):
        with pytest.raises(AppError) as exc:
            operation()
        assert exc.value.code.value == "PERMISSION_DENIED"


def test_location_scoped_grant_only_covers_its_location(session):
    from app.scheduling.service import list_appointments

    ids = seed_booking(session)
    other = Location(
        organization_id=ORG, name="Sede Otra", timezone=LIMA, is_active=True
    )
    session.add(other)
    session.commit()

    principal = create_principal(session, display_name="loc", principal_type="human")
    membership = add_membership(session, organization_id=ORG, principal_id=principal.id)
    role = create_role(session, organization_id=ORG, code="loc-role", name="loc")
    grant_permission(session, role_id=role.id, permission_code=APPOINTMENTS_READ)
    assign_role(
        session,
        organization_id=ORG,
        membership_id=membership.id,
        role_id=role.id,
        location_id=ids["location_id"],
    )
    session.rollback()

    ctx = ctx_for(principal.id)
    in_scope = list_appointments(session, ctx=ctx, location_id=ids["location_id"])
    assert in_scope == []
    with pytest.raises(AppError) as exc:
        list_appointments(session, ctx=ctx, location_id=other.id)
    assert exc.value.code.value == "PERMISSION_DENIED"
    # An org-wide request is not satisfied by a location-scoped grant (E5).
    with pytest.raises(AppError) as exc:
        list_appointments(session, ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"


# --- agenda journey (book → list → reload → reschedule → cancel) -------------


def test_agenda_journey_over_http(client, session):
    ids = seed_booking(session)

    listed_before = client.get("/appointments").json()
    assert listed_before == []

    booked = book(client, ids, start=utc_of(9), key="journey-book")
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["id"]

    # Replay with the same key returns the same logical outcome.
    replay = book(client, ids, start=utc_of(9), key="journey-book")
    assert replay.status_code == 201
    assert replay.json()["id"] == appointment_id

    rows = client.get("/appointments").json()
    assert len(rows) == 1
    assert rows[0]["id"] == appointment_id

    detail = client.get(f"/appointments/{appointment_id}")
    assert detail.status_code == 200
    assert detail.json()["start_utc"].startswith("2026-08-10T")

    rescheduled = client.post(
        f"/appointments/{appointment_id}/reschedule",
        json={"new_start": utc_of(10).isoformat()},
        headers={"Idempotency-Key": "journey-resched"},
    )
    assert rescheduled.status_code == 200, rescheduled.text
    assert datetime.fromisoformat(rescheduled.json()["start_utc"]) == utc_of(10)

    after_reschedule = client.get(f"/appointments/{appointment_id}").json()
    assert datetime.fromisoformat(after_reschedule["start_utc"]) == utc_of(10)

    cancelled = client.post(
        f"/appointments/{appointment_id}/cancel",
        headers={"Idempotency-Key": "journey-cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"

    after_cancel = client.get(f"/appointments/{appointment_id}").json()
    assert after_cancel["state"] == "cancelled"

    # Exactly one appointment, one receipt per mutation, audit trail intact.
    from app.idempotency.models import CommandReceipt
    from sqlalchemy import func, select

    assert session.scalar(select(func.count()).select_from(Appointment)) == 1
    assert (
        session.scalar(select(func.count()).select_from(CommandReceipt)) == 3
    )  # book + reschedule + cancel
    session.rollback()
