"""PF1 — Organization & tenant integrity proofs against real PostgreSQL.

Every proof in the first section deliberately **bypasses the application
services** and writes the forbidden row directly, because the invariant under
test is a database invariant (PF0 A1/§7): a cross-tenant relational state must
be impossible even if every application check were absent, bypassed or buggy.

The later sections prove the behavioural half: a global practitioner working
for two organizations, tenant-scoped reads, the practitioner-global overlap
invariant surviving multi-tenancy, and tenant-attributed audit rows.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.catalog.schemas import ServiceCreate
from app.catalog.service import create_service, list_services
from app.commercial.schemas import LeadCreate
from app.commercial.service import create_lead, get_lead
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.organization.models import (
    Location,
    Organization,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.schemas import CapabilityCreate, LocationCreate, PractitionerCreate
from app.organization.service import (
    add_practitioner_membership,
    create_capability,
    create_location,
    create_organization,
    create_practitioner,
    list_eligible_practitioners,
)
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock
from app.scheduling.query import (
    create_availability_rule,
    create_schedule_block,
    find_available_slots,
)
from app.scheduling.schemas import AvailabilityRuleCreate, ScheduleBlockCreate
from app.scheduling.service import (
    book_appointment,
    cancel_appointment,
    reschedule_appointment,
)
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)  # weekday 0, the anchor day used since Task 6
RULE_WINDOW = (time(9, 0), time(13, 0))


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed_org(
    session,
    name,
    *,
    service_name="Limpieza dental",
    duration_minutes=30,
    practitioner_id=None,
    practitioner_name="Dra. Ana",
    organization_id=None,
):
    """Build one complete, bookable tenant through the application services.

    ``practitioner_id`` reuses an existing **global** practitioner and only adds
    the membership, which is how a professional comes to work for a second
    organization (PF0 PM1).
    """
    if organization_id is None:
        organization_id = create_organization(session, name).id
    service = create_service(
        session,
        ServiceCreate(name=service_name, duration_minutes=duration_minutes),
        organization_id,
    )
    location = create_location(
        session, LocationCreate(name=f"Sede {name}", timezone=LIMA), organization_id
    )
    if practitioner_id is None:
        practitioner = create_practitioner(
            session, PractitionerCreate(display_name=practitioner_name), organization_id
        )
    else:
        add_practitioner_membership(session, practitioner_id, organization_id)
        practitioner = session.get(Practitioner, practitioner_id)
    lead = create_lead(
        session,
        LeadCreate(
            full_name=f"Paciente {name}",
            contact_phone="+51999000111",
            acquisition_source="direct",
        ),
        organization_id,
    )
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=practitioner.id,
            service_id=service.id,
            location_id=location.id,
        ),
        organization_id,
    )
    create_availability_rule(
        session,
        AvailabilityRuleCreate(
            practitioner_id=practitioner.id,
            location_id=location.id,
            day_of_week=0,
            start_local=RULE_WINDOW[0],
            end_local=RULE_WINDOW[1],
        ),
        organization_id,
    )
    ids = {
        "organization_id": organization_id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
        "lead_id": lead.id,
    }
    # Hand back plain ids and leave the session idle: the configuration helpers
    # end with a refresh, which opens a fresh transaction, and booking demands an
    # idle Session (PF0 A2).
    session.rollback()
    return ids


@pytest.fixture
def two_orgs(session):
    """Two independent tenants, each with its own catalog and practitioner."""
    a = seed_org(session, "Alfa", practitioner_name="Dra. Ana")
    b = seed_org(session, "Beta", practitioner_name="Dr. Luis")
    assert a["organization_id"] != b["organization_id"]
    return a, b


APPOINTMENT_INSERT = (
    "INSERT INTO appointments (organization_id, lead_id, service_id, practitioner_id,"
    " location_id, start_utc, end_utc, state)"
    " VALUES (:organization_id, :lead_id, :service_id, :practitioner_id,"
    "         :location_id, :start_utc, :end_utc, :state)"
)


def refused_by_database(session, statement, params=None) -> str:
    """Run one raw statement in its own transaction; return the DB error text.

    Raw SQL on purpose: the invariant under test must hold with every
    application check absent (PF0 A1). The session is left idle afterwards so the
    caller can keep asserting.
    """
    session.rollback()
    with pytest.raises(IntegrityError) as exc:
        with session.begin():
            session.execute(text(statement), params or {})
    session.rollback()
    return str(exc.value.orig)


def appointment_columns(org, *, lead, service, practitioner, location, start=None):
    start_utc = start or utc_of(9)
    return {
        "organization_id": org,
        "lead_id": lead,
        "service_id": service,
        "practitioner_id": practitioner,
        "location_id": location,
        "start_utc": start_utc,
        "end_utc": start_utc + timedelta(minutes=30),
        "state": "confirmed",
    }


# --- 1. the canonical §7 case: appointments cannot mix tenants --------------


def test_appointment_cannot_reference_another_tenants_service(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        APPOINTMENT_INSERT,
        appointment_columns(
            a["organization_id"],
            lead=a["lead_id"],
            service=b["service_id"],  # organization B's catalog entry
            practitioner=a["practitioner_id"],
            location=a["location_id"],
        ),
    )
    assert "fk_appointments_organization_service" in error
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0


def test_appointment_cannot_reference_another_tenants_lead(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        APPOINTMENT_INSERT,
        appointment_columns(
            a["organization_id"],
            lead=b["lead_id"],
            service=a["service_id"],
            practitioner=a["practitioner_id"],
            location=a["location_id"],
        ),
    )
    assert "fk_appointments_organization_lead" in error


def test_appointment_cannot_reference_another_tenants_location(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        APPOINTMENT_INSERT,
        appointment_columns(
            a["organization_id"],
            lead=a["lead_id"],
            service=a["service_id"],
            practitioner=a["practitioner_id"],
            location=b["location_id"],
        ),
    )
    assert "fk_appointments_organization_location" in error


def test_appointment_cannot_name_a_practitioner_without_membership(session, two_orgs):
    """PF0 F-3b: a professional who does not work here cannot be scheduled here."""
    a, b = two_orgs
    error = refused_by_database(
        session,
        APPOINTMENT_INSERT,
        appointment_columns(
            a["organization_id"],
            lead=a["lead_id"],
            service=a["service_id"],
            practitioner=b["practitioner_id"],  # a member of B only
            location=a["location_id"],
        ),
    )
    assert "fk_appointments_organization_membership" in error


def test_appointment_requires_a_tenant(session, two_orgs):
    a, _b = two_orgs
    error = refused_by_database(
        session,
        "INSERT INTO appointments (lead_id, service_id, practitioner_id, location_id,"
        " start_utc, end_utc, state)"
        " VALUES (:lead_id, :service_id, :practitioner_id, :location_id,"
        "         :start_utc, :end_utc, 'confirmed')",
        {
            "lead_id": a["lead_id"],
            "service_id": a["service_id"],
            "practitioner_id": a["practitioner_id"],
            "location_id": a["location_id"],
            "start_utc": utc_of(9),
            "end_utc": utc_of(9, 30),
        },
    )
    assert 'null value in column "organization_id"' in error


# --- 2. capabilities cannot mix tenant resources ----------------------------


CAPABILITY_INSERT = (
    "INSERT INTO practitioner_capabilities (organization_id, practitioner_id,"
    " service_id, location_id) VALUES (:org, :practitioner, :service, :location)"
)


def test_capability_cannot_mix_service_and_location_of_different_tenants(
    session, two_orgs
):
    a, b = two_orgs
    error = refused_by_database(
        session,
        CAPABILITY_INSERT,
        {
            "org": a["organization_id"],
            "practitioner": a["practitioner_id"],
            "service": a["service_id"],
            "location": b["location_id"],  # organization B's branch
        },
    )
    assert "fk_capabilities_organization_location" in error


def test_capability_cannot_reference_another_tenants_service(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        CAPABILITY_INSERT,
        {
            "org": a["organization_id"],
            "practitioner": a["practitioner_id"],
            "service": b["service_id"],
            "location": a["location_id"],
        },
    )
    assert "fk_capabilities_organization_service" in error


def test_capability_cannot_name_a_non_member_practitioner(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        CAPABILITY_INSERT,
        {
            "org": a["organization_id"],
            "practitioner": b["practitioner_id"],
            "service": a["service_id"],
            "location": a["location_id"],
        },
    )
    assert "fk_capabilities_organization_membership" in error


def test_create_capability_refuses_a_cross_tenant_service(session, two_orgs):
    """The application layer produces the stable contract error before the DB."""
    a, b = two_orgs
    with pytest.raises(AppError) as exc:
        create_capability(
            session,
            CapabilityCreate(
                practitioner_id=a["practitioner_id"],
                service_id=b["service_id"],
                location_id=a["location_id"],
            ),
            a["organization_id"],
        )
    assert exc.value.code is ErrorCode.NOT_FOUND


# --- 3. availability rules and schedule blocks cannot mix org/location ------


AVAILABILITY_RULE_INSERT = (
    "INSERT INTO availability_rules (organization_id, practitioner_id, location_id,"
    " day_of_week, start_local, end_local)"
    " VALUES (:org, :practitioner, :location, 0, '09:00', '13:00')"
)


def test_availability_rule_cannot_use_another_tenants_location(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        AVAILABILITY_RULE_INSERT,
        {
            "org": a["organization_id"],
            "practitioner": a["practitioner_id"],
            "location": b["location_id"],
        },
    )
    assert "fk_availability_rules_organization_location" in error


def test_availability_rule_cannot_name_a_non_member_practitioner(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        AVAILABILITY_RULE_INSERT,
        {
            "org": a["organization_id"],
            "practitioner": b["practitioner_id"],
            "location": a["location_id"],
        },
    )
    assert "fk_availability_rules_organization_membership" in error


def test_schedule_block_cannot_use_another_tenants_location(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        "INSERT INTO schedule_blocks (organization_id, practitioner_id, location_id,"
        " start_utc, end_utc) VALUES (:org, :practitioner, :location, :start, :end)",
        {
            "org": a["organization_id"],
            "practitioner": a["practitioner_id"],
            "location": b["location_id"],
            "start": utc_of(9),
            "end": utc_of(10),
        },
    )
    assert "fk_schedule_blocks_organization_location" in error


def test_schedule_block_service_refuses_a_cross_tenant_location(session, two_orgs):
    a, b = two_orgs
    with pytest.raises(AppError) as exc:
        create_schedule_block(
            session,
            ScheduleBlockCreate(
                practitioner_id=a["practitioner_id"],
                location_id=b["location_id"],
                start_utc=utc_of(9),
                end_utc=utc_of(10),
            ),
            a["organization_id"],
        )
    assert exc.value.code is ErrorCode.NOT_FOUND
    assert session.scalar(select(func.count()).select_from(ScheduleBlock)) == 0


# --- 4. lead ownership and the nullable composite FK (MATCH SIMPLE) ---------


def test_lead_cannot_need_another_tenants_service(session, two_orgs):
    a, b = two_orgs
    error = refused_by_database(
        session,
        "INSERT INTO leads (organization_id, full_name, contact_phone,"
        " acquisition_source, service_need_id)"
        " VALUES (:org, 'Cross', '+51999', 'direct', :service)",
        {"org": a["organization_id"], "service": b["service_id"]},
    )
    assert "fk_leads_organization_service_need" in error


def test_lead_without_service_need_satisfies_the_composite_fk(session, two_orgs):
    """MATCH SIMPLE (§7.3): the check is skipped when the optional service is NULL."""
    a, _b = two_orgs
    lead = create_lead(
        session,
        LeadCreate(
            full_name="Sin necesidad",
            contact_email="sin@example.com",
            acquisition_source="direct",
        ),
        a["organization_id"],
    )
    assert lead.service_need_id is None
    assert lead.organization_id == a["organization_id"]


def test_create_lead_refuses_a_cross_tenant_service_need(session, two_orgs):
    a, b = two_orgs
    with pytest.raises(AppError) as exc:
        create_lead(
            session,
            LeadCreate(
                full_name="Cross",
                contact_phone="+51999000111",
                acquisition_source="promotion",
                service_need_id=b["service_id"],
            ),
            a["organization_id"],
        )
    assert exc.value.code is ErrorCode.NOT_FOUND


# --- 5. service name uniqueness is per organization -------------------------


def test_same_service_name_allowed_in_two_organizations(session, two_orgs):
    a, b = two_orgs
    first = create_service(
        session, ServiceCreate(name="Blanqueamiento", duration_minutes=60), a["organization_id"]
    )
    second = create_service(
        session, ServiceCreate(name="Blanqueamiento", duration_minutes=45), b["organization_id"]
    )
    assert first.id != second.id
    assert first.organization_id == a["organization_id"]
    assert second.organization_id == b["organization_id"]
    assert second.duration_minutes == 45  # independent catalogs, not a shared row


def test_duplicate_service_name_inside_one_organization_is_rejected(session, two_orgs):
    a, _b = two_orgs
    create_service(
        session, ServiceCreate(name="Blanqueamiento", duration_minutes=60), a["organization_id"]
    )
    with pytest.raises(AppError) as exc:
        create_service(
            session,
            ServiceCreate(name="Blanqueamiento", duration_minutes=30),
            a["organization_id"],
        )
    assert exc.value.code is ErrorCode.INVALID_INPUT
    # And the database refuses it too, with the application check bypassed.
    error = refused_by_database(
        session,
        "INSERT INTO services (organization_id, name, duration_minutes)"
        " VALUES (:org, 'Blanqueamiento', 30)",
        {"org": a["organization_id"]},
    )
    assert "uq_services_organization_name" in error


def test_global_service_name_unique_constraint_is_gone(session):
    names = {
        row[0]
        for row in session.execute(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'services'::regclass AND contype = 'u'"
            )
        )
    }
    session.rollback()
    assert "services_name_key" not in names
    assert {"uq_services_organization_name", "uq_services_organization_id"} <= names


# --- 6. a global practitioner working for two organizations -----------------


def test_practitioner_can_hold_memberships_in_two_organizations(session):
    a = seed_org(session, "Alfa", practitioner_name="Dra. Ana")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    memberships = list(
        session.scalars(
            select(PractitionerMembership)
            .where(PractitionerMembership.practitioner_id == a["practitioner_id"])
            .order_by(PractitionerMembership.organization_id)
        )
    )
    assert [m.organization_id for m in memberships] == [
        a["organization_id"],
        b["organization_id"],
    ]
    # One global identity row, two tenant reaches (PF0 T2/PM1).
    assert session.scalar(select(func.count()).select_from(Practitioner)) == 1
    assert list_eligible_practitioners(
        session, a["service_id"], a["location_id"], a["organization_id"]
    ) == [session.get(Practitioner, a["practitioner_id"])]
    assert [
        p.id
        for p in list_eligible_practitioners(
            session, b["service_id"], b["location_id"], b["organization_id"]
        )
    ] == [a["practitioner_id"]]


def test_shared_practitioner_is_bookable_in_both_organizations(session):
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    first = book_appointment(
        session,
        lead_id=a["lead_id"],
        service_id=a["service_id"],
        location_id=a["location_id"],
        practitioner_id=a["practitioner_id"],
        start=local(9),
        organization_id=a["organization_id"],
    )
    second = book_appointment(
        session,
        lead_id=b["lead_id"],
        service_id=b["service_id"],
        location_id=b["location_id"],
        practitioner_id=b["practitioner_id"],
        start=local(10),
        organization_id=b["organization_id"],
    )
    assert first.organization_id == a["organization_id"]
    assert second.organization_id == b["organization_id"]
    assert first.practitioner_id == second.practitioner_id


def test_membership_deactivation_removes_eligibility_in_that_organization_only(session):
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    membership_a = session.scalars(
        select(PractitionerMembership).where(
            PractitionerMembership.organization_id == a["organization_id"],
            PractitionerMembership.practitioner_id == a["practitioner_id"],
        )
    ).one()
    membership_a.is_active = False
    session.commit()

    assert (
        list_eligible_practitioners(
            session, a["service_id"], a["location_id"], a["organization_id"]
        )
        == []
    )
    assert [
        p.id
        for p in list_eligible_practitioners(
            session, b["service_id"], b["location_id"], b["organization_id"]
        )
    ] == [a["practitioner_id"]]

    session.rollback()  # the eligibility reads above own a transaction
    with pytest.raises(AppError) as exc:
        book_appointment(
            session,
            lead_id=a["lead_id"],
            service_id=a["service_id"],
            location_id=a["location_id"],
            practitioner_id=a["practitioner_id"],
            start=local(9),
            organization_id=a["organization_id"],
        )
    assert exc.value.code is ErrorCode.ENTITY_INACTIVE


def test_global_deactivation_removes_eligibility_everywhere(session):
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    practitioner = session.get(Practitioner, a["practitioner_id"])
    practitioner.is_active = False
    session.commit()

    for org in (a, b):
        assert (
            list_eligible_practitioners(
                session, org["service_id"], org["location_id"], org["organization_id"]
            )
            == []
        )


def test_organization_a_cannot_schedule_a_practitioner_it_does_not_employ(
    session, two_orgs
):
    a, b = two_orgs
    with pytest.raises(AppError) as exc:
        book_appointment(
            session,
            lead_id=a["lead_id"],
            service_id=a["service_id"],
            location_id=a["location_id"],
            practitioner_id=b["practitioner_id"],
            start=local(9),
            organization_id=a["organization_id"],
        )
    assert exc.value.code is ErrorCode.NOT_FOUND  # not even resolvable here (T5)


# --- 7. the practitioner-global overlap invariant survives tenancy ----------


def test_confirmed_appointment_in_one_org_blocks_the_interval_in_the_other(session):
    """PF0 §9 S2: a shared practitioner cannot be in two chairs at 09:00."""
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    book_appointment(
        session,
        lead_id=a["lead_id"],
        service_id=a["service_id"],
        location_id=a["location_id"],
        practitioner_id=a["practitioner_id"],
        start=local(9),
        organization_id=a["organization_id"],
    )

    with pytest.raises(AppError) as exc:
        book_appointment(
            session,
            lead_id=b["lead_id"],
            service_id=b["service_id"],
            location_id=b["location_id"],
            practitioner_id=b["practitioner_id"],
            start=local(9),
            organization_id=b["organization_id"],
        )
    # Preflight — not a raw 23P01: the conflicting-appointment read is still
    # practitioner-global, so it agrees with the GiST exactly (§9 S4, F-16).
    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert exc.value.details == {}
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1


def test_cross_organization_overlap_is_rejected_by_the_gist_when_preflight_bypassed(
    session,
):
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    book_appointment(
        session,
        lead_id=a["lead_id"],
        service_id=a["service_id"],
        location_id=a["location_id"],
        practitioner_id=a["practitioner_id"],
        start=local(9),
        organization_id=a["organization_id"],
    )

    session.rollback()
    with pytest.raises(IntegrityError) as exc:
        with session.begin():
            session.execute(
                text(APPOINTMENT_INSERT),
                appointment_columns(
                    b["organization_id"],
                    lead=b["lead_id"],
                    service=b["service_id"],
                    practitioner=b["practitioner_id"],
                    location=b["location_id"],
                    start=utc_of(9, 15),
                ),
            )
    # The tenant-agnostic GiST exclusion settles it, exactly as within one
    # organization: SQLSTATE 23P01, which the transport maps to 409.
    assert exc.value.orig.sqlstate == "23P01"
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1


def test_slot_query_hides_an_interval_taken_by_the_other_organization(session):
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])

    book_appointment(
        session,
        lead_id=a["lead_id"],
        service_id=a["service_id"],
        location_id=a["location_id"],
        practitioner_id=a["practitioner_id"],
        start=local(9),
        organization_id=a["organization_id"],
    )

    slots = find_available_slots(
        session,
        b["service_id"],
        b["location_id"],
        utc_of(0),
        utc_of(0, day=MONDAY + timedelta(days=1)),
        b["organization_id"],
    )
    starts = {slot["start"] for slot in slots}
    assert utc_of(9) not in starts  # the shared practitioner is busy
    assert utc_of(10) in starts  # the rest of B's own window is untouched


def test_cross_organization_conflict_leaks_nothing_through_http(session, migrated_engine):
    """The 409 envelope carries ``details = {}`` and no other tenant's data (S3)."""
    other = seed_org(session, "Beta")
    bootstrap = seed_org(
        session,
        "Bootstrap",
        practitioner_id=other["practitioner_id"],
        organization_id=BOOTSTRAP_ORGANIZATION_ID,
    )
    book_appointment(
        session,
        lead_id=other["lead_id"],
        service_id=other["service_id"],
        location_id=other["location_id"],
        practitioner_id=other["practitioner_id"],
        start=local(9),
        organization_id=other["organization_id"],
    )

    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/appointments",
        json={
            "lead_id": bootstrap["lead_id"],
            "service_id": bootstrap["service_id"],
            "location_id": bootstrap["location_id"],
            "practitioner_id": bootstrap["practitioner_id"],
            "start": utc_of(9).isoformat(),
        },
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "SLOT_BLOCKED"
    assert body["error"]["details"] == {}
    assert "Sede Beta" not in response.text
    assert str(other["organization_id"]) not in body["error"]["message"]


# --- 8. tenant-scoped reads -------------------------------------------------


def test_service_list_is_tenant_scoped(session, two_orgs):
    a, b = two_orgs
    assert [s.id for s in list_services(session, a["organization_id"])] == [
        a["service_id"]
    ]
    assert [s.id for s in list_services(session, b["organization_id"])] == [
        b["service_id"]
    ]


def test_lead_read_is_tenant_scoped(session, two_orgs):
    a, b = two_orgs
    assert get_lead(session, a["lead_id"], a["organization_id"]).id == a["lead_id"]
    with pytest.raises(AppError) as exc:
        get_lead(session, b["lead_id"], a["organization_id"])
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_eligibility_and_slot_reads_are_tenant_scoped(session, two_orgs):
    a, b = two_orgs
    with pytest.raises(AppError) as exc:
        list_eligible_practitioners(
            session, b["service_id"], b["location_id"], a["organization_id"]
        )
    assert exc.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(AppError) as slots_exc:
        find_available_slots(
            session,
            b["service_id"],
            b["location_id"],
            utc_of(0),
            utc_of(0, day=MONDAY + timedelta(days=1)),
            a["organization_id"],
        )
    assert slots_exc.value.code is ErrorCode.NOT_FOUND


def test_appointment_mutations_are_tenant_scoped(session, two_orgs):
    a, b = two_orgs
    appointment = book_appointment(
        session,
        lead_id=b["lead_id"],
        service_id=b["service_id"],
        location_id=b["location_id"],
        practitioner_id=b["practitioner_id"],
        start=local(9),
        organization_id=b["organization_id"],
    )

    appointment_id = appointment.id
    for operation in (
        lambda: cancel_appointment(
            session, appointment_id, organization_id=a["organization_id"]
        ),
        lambda: reschedule_appointment(
            session, appointment_id, local(10), organization_id=a["organization_id"]
        ),
    ):
        with pytest.raises(AppError) as exc:
            operation()
        assert exc.value.code is ErrorCode.NOT_FOUND

    stored = session.get(Appointment, appointment_id)
    session.rollback()
    assert stored.state == "confirmed"
    assert stored.start_utc == utc_of(9)


def test_availability_rules_are_published_per_organization(session):
    a = seed_org(session, "Alfa")
    b = seed_org(session, "Beta", practitioner_id=a["practitioner_id"])
    rules = list(
        session.scalars(
            select(AvailabilityRule).order_by(AvailabilityRule.organization_id)
        )
    )
    assert [rule.organization_id for rule in rules] == [
        a["organization_id"],
        b["organization_id"],
    ]
    assert rules[0].location_id != rules[1].location_id


# --- 9. tenant-attributed audit --------------------------------------------


def test_appointment_lifecycle_audit_carries_the_acting_organization(session, two_orgs):
    _a, b = two_orgs
    appointment = book_appointment(
        session,
        lead_id=b["lead_id"],
        service_id=b["service_id"],
        location_id=b["location_id"],
        practitioner_id=b["practitioner_id"],
        start=local(9),
        organization_id=b["organization_id"],
    )
    reschedule_appointment(
        session, appointment.id, local(10), organization_id=b["organization_id"]
    )
    cancel_appointment(session, appointment.id, organization_id=b["organization_id"])

    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == "appointment")
            .order_by(AuditEvent.id)
        )
    )
    session.rollback()
    assert [event.action for event in events] == [
        "appointment.created",
        "appointment.rescheduled",
        "appointment.cancelled",
    ]
    assert {event.organization_id for event in events} == {b["organization_id"]}


def test_organization_creation_is_audited_against_its_own_id(session):
    organization = create_organization(session, "Gamma")

    event = session.scalars(
        select(AuditEvent).where(AuditEvent.entity_type == "organization")
    ).one()
    session.rollback()
    assert event.action == "organization.created"
    assert event.entity_id == str(organization.id)
    assert event.organization_id == organization.id  # PF0 D7 self-reference


def test_audit_event_requires_an_existing_tenant(session, two_orgs):
    unknown_tenant = refused_by_database(
        session,
        "INSERT INTO audit_events (organization_id, actor_id, actor_type, action,"
        " entity_id, entity_type)"
        " VALUES (999999, 'system', 'system', 'appointment.created', '1', 'appointment')",
    )
    assert "fk_audit_events_organization" in unknown_tenant

    no_tenant = refused_by_database(
        session,
        "INSERT INTO audit_events (actor_id, actor_type, action, entity_id, entity_type)"
        " VALUES ('system', 'system', 'appointment.created', '1', 'appointment')",
    )
    assert 'null value in column "organization_id"' in no_tenant


# --- 10. bootstrap tenant and ownership coverage ---------------------------


def test_bootstrap_organization_is_the_resolved_default(session):
    organization = session.get(Organization, BOOTSTRAP_ORGANIZATION_ID)
    assert organization is not None
    # No organization_id supplied anywhere: the single seam resolves it.
    service = create_service(session, ServiceCreate(name="Limpieza", duration_minutes=30))
    location = create_location(
        session, LocationCreate(name="Sede Centro", timezone=LIMA)
    )
    lead = create_lead(
        session,
        LeadCreate(
            full_name="Juan", contact_phone="+51999000111", acquisition_source="direct"
        ),
    )
    practitioner = create_practitioner(session, PractitionerCreate(display_name="Dra. Ana"))
    membership = session.scalars(
        select(PractitionerMembership).where(
            PractitionerMembership.practitioner_id == practitioner.id
        )
    ).one()
    assert {
        service.organization_id,
        location.organization_id,
        lead.organization_id,
        membership.organization_id,
    } == {BOOTSTRAP_ORGANIZATION_ID}


def test_every_tenant_owned_table_carries_a_not_null_organization_id(session):
    rows = session.execute(
        text(
            "SELECT table_name, is_nullable FROM information_schema.columns"
            " WHERE table_schema = 'public' AND column_name = 'organization_id'"
            " ORDER BY table_name"
        )
    ).all()
    session.rollback()
    assert {row[0] for row in rows} == {
        "appointments",
        "audit_events",
        "availability_rules",
        "leads",
        "locations",
        "practitioner_capabilities",
        "practitioner_memberships",
        "schedule_blocks",
        "services",
        # PF2 tenant-owned tables (same rule, same NOT NULL).
        "memberships",
        "roles",
        "role_assignments",
        # PF4 — the receipt claim is tenant-scoped by construction (I2).
        "command_receipts",
        # PF5 — clinical tables carry the same direct tenant column (T1/P10).
        "patients",
        "visits",
        "service_executions",
        # PF6 — economic & operations tables (T1).
        "products",
        "service_consumptions",
        "charges",
        "payments",
    }
    assert {row[1] for row in rows} == {"NO"}
    # Practitioner and Principal stay global: no tenant column at all, they
    # reach a tenant only through their membership row (PF0 T2/P4/PR3).
    assert not [row for row in rows if row[0] in {"practitioners", "principals"}]
    # `permissions` is a platform catalog, never tenant data (T3/M5).
    assert not [row for row in rows if row[0] == "permissions"]


def test_practitioner_membership_is_unique_per_organization(session, two_orgs):
    a, _b = two_orgs
    error = refused_by_database(
        session,
        "INSERT INTO practitioner_memberships (organization_id, practitioner_id)"
        " VALUES (:org, :practitioner)",
        {"org": a["organization_id"], "practitioner": a["practitioner_id"]},
    )
    assert "uq_practitioner_memberships_org_practitioner" in error


def test_gist_exclusion_and_capability_unique_are_unchanged(session):
    definitions = dict(
        session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname IN ('excl_appointments_confirmed_no_overlap',"
                "                   'uq_capabilities_practitioner_service_location')"
            )
        ).all()
    )
    session.rollback()
    assert definitions["excl_appointments_confirmed_no_overlap"] == (
        "EXCLUDE USING gist (practitioner_id WITH =,"
        " tstzrange(start_utc, end_utc, '[)'::text) WITH &&)"
        " WHERE (((state)::text = 'confirmed'::text))"
    )
    # organization_id is deliberately absent from both keys (§9 S1, PM7).
    assert "organization_id" not in definitions["excl_appointments_confirmed_no_overlap"]
    assert definitions["uq_capabilities_practitioner_service_location"] == (
        "UNIQUE (practitioner_id, service_id, location_id)"
    )


def test_service_and_location_stay_reachable_from_one_tenant_only(session, two_orgs):
    """No row is reachable from two organizations (PF0 T4)."""
    a, b = two_orgs
    a_service = session.get(Service, a["service_id"])
    b_location = session.get(Location, b["location_id"])
    session.rollback()
    assert a_service.organization_id == a["organization_id"]
    assert b_location.organization_id == b["organization_id"]
    assert (
        session.scalar(
            select(func.count())
            .select_from(PractitionerCapability)
            .where(PractitionerCapability.organization_id == a["organization_id"])
        )
        == 1
    )
