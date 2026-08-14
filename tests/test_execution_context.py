"""PF3 — ExecutionContext & audit provenance proofs against real PostgreSQL.

The block makes ``ExecutionContext`` the explicit application-boundary
contract (PF0 §13): transport adapters derive it per request, mutating services
receive it, and ``record_event`` writes tenant / principal / correlation
provenance from it (D2/D3). These tests prove the contract, the HTTP derivation
rules, the authorization wiring, and that audit provenance stays atomic with the
mutation it describes.
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
from app.context import default_context, resolve_http_context
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.models import SYSTEM_PRINCIPAL_ID, Membership, Principal, Role
from app.iam.permissions import (
    APPOINTMENTS_CANCEL,
    APPOINTMENTS_CREATE,
    APPOINTMENTS_READ,
    APPOINTMENTS_RESCHEDULE,
)
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
    has_permission,
    set_membership_active,
)
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.schemas import LocationCreate
from app.organization.service import create_location, create_organization
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock
from app.scheduling.service import (
    book_appointment,
    cancel_appointment,
    reschedule_appointment,
)
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)  # weekday 0, the anchor day used since Task 6
RULE_WINDOW = (time(9, 0), time(13, 0))


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed_booking(session, *, organization_id=ORG):
    """One complete, bookable tenant chain through the application services."""
    service = Service(
        organization_id=organization_id,
        name=f"Servicio {organization_id}",
        duration_minutes=30,
        is_active=True,
    )
    location = Location(
        organization_id=organization_id,
        name=f"Sede {organization_id}",
        timezone=LIMA,
        is_active=True,
    )
    practitioner = Practitioner(display_name="Dra. Ana", is_active=True)
    lead = Lead(
        organization_id=organization_id,
        full_name="Juan Pérez",
        contact_phone="+51999000111",
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


def seed_actor(session, *, organization_id=ORG, principal_type="human", codes=()):
    """A tenant principal holding the given permission codes, org-wide.

    Returns plain values (principal id, principal type, membership id) so the
    session is left **idle**: the IAM provisioning helpers end with a refresh,
    which opens a transaction, and the mutation under test demands an idle
    Session (PF0 A2).
    """
    principal = create_principal(
        session,
        display_name=f"{principal_type} actor",
        principal_type=principal_type,
    )
    membership = add_membership(session, organization_id=organization_id, principal_id=principal.id)
    role = create_role(
        session, organization_id=organization_id, code=f"role-{principal_type}", name=principal_type
    )
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(
        session,
        organization_id=organization_id,
        membership_id=membership.id,
        role_id=role.id,
    )
    values = (principal.id, principal.type, membership.id)
    session.rollback()
    return values


def ctx_for(principal_id, principal_type, organization_id, *, correlation_id="corr-test"):
    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal_id,
        principal_type=principal_type,
        request_id="req-test",
        correlation_id=correlation_id,
    )


def audit_rows(session, action=None):
    query = select(AuditEvent).order_by(AuditEvent.id)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    rows = list(session.scalars(query))
    session.rollback()
    return rows


def count_of(session, model):
    total = session.scalar(select(func.count()).select_from(model))
    session.rollback()
    return total


# --- 1. ExecutionContext contract (PF0 §13 X1/X2) ---------------------------


def test_execution_context_carries_all_required_fields():
    ctx = ExecutionContext(
        organization_id=ORG,
        principal_id=SYSTEM_PRINCIPAL_ID,
        principal_type="system",
        request_id="req-1",
        correlation_id="corr-1",
    )
    assert ctx.organization_id == ORG
    assert ctx.principal_id == SYSTEM_PRINCIPAL_ID
    assert ctx.principal_type == "system"
    assert ctx.request_id == "req-1"
    assert ctx.correlation_id == "corr-1"


def test_default_context_is_the_trusted_system_principal(session):
    ctx = default_context()
    assert ctx.organization_id == ORG
    assert ctx.principal_id == SYSTEM_PRINCIPAL_ID
    assert ctx.principal_type == "system"
    assert ctx.request_id
    assert ctx.correlation_id == ctx.request_id  # X5: never NULL


# --- 2-3. HTTP boundary derivation (PF0 §13 X3) ------------------------------


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


def _book_payload(ids, start_utc):
    return {
        "lead_id": ids["lead_id"],
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "practitioner_id": ids["practitioner_id"],
        "start": start_utc.isoformat(),
    }


def test_request_id_is_unique_per_http_request(client, session):
    ids = seed_booking(session)

    first = client.post("/appointments", json=_book_payload(ids, utc_of(9)))
    second = client.post("/appointments", json=_book_payload(ids, utc_of(10)))
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    events = audit_rows(session, action="appointment.created")
    assert len(events) == 2
    # No X-Correlation-Id header was sent, so correlation == request_id (X5),
    # and the two requests must have produced different request_ids.
    assert events[0].correlation_id is not None
    assert events[0].correlation_id != events[1].correlation_id


def test_supplied_correlation_id_propagates_into_audit(client, session):
    ids = seed_booking(session)

    response = client.post(
        "/appointments",
        json=_book_payload(ids, utc_of(9)),
        headers={"X-Correlation-Id": "trace-abc"},
    )
    assert response.status_code == 201, response.text

    event = audit_rows(session, action="appointment.created")[0]
    assert event.correlation_id == "trace-abc"


def test_absent_correlation_derives_from_request_id(client, session):
    ids = seed_booking(session)

    first = client.post("/appointments", json=_book_payload(ids, utc_of(9)))
    second = client.post("/appointments", json=_book_payload(ids, utc_of(10)))
    assert first.status_code == 201 and second.status_code == 201

    events = audit_rows(session, action="appointment.created")
    assert all(event.correlation_id is not None for event in events)
    assert events[0].correlation_id != events[1].correlation_id


# --- 4-6. principal and tenant provenance ------------------------------------


def test_authorized_human_action_records_human_principal_provenance(session):
    ids = seed_booking(session)
    human_id, human_type, _membership = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(human_id, human_type, ORG, correlation_id="corr-human")

    book_appointment(session, ctx=ctx, start=local(9), **{k: v for k, v in ids.items() if k != "organization_id"})

    event = audit_rows(session, action="appointment.created")[0]
    assert event.actor_id == str(human_id)
    assert event.actor_type == "human"
    assert event.correlation_id == "corr-human"


def test_authorized_agent_action_records_agent_principal_provenance(session):
    ids = seed_booking(session)
    agent_id, agent_type, _membership = seed_actor(
        session, principal_type="agent", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(agent_id, agent_type, ORG, correlation_id="corr-agent")

    book_appointment(session, ctx=ctx, start=local(9), **{k: v for k, v in ids.items() if k != "organization_id"})

    event = audit_rows(session, action="appointment.created")[0]
    assert event.actor_id == str(agent_id)
    assert event.actor_type == "agent"
    assert event.correlation_id == "corr-agent"


def test_organization_attribution_comes_from_the_context(session):
    other_id = create_organization(session, "Otra Clinica").id
    ids = seed_booking(session, organization_id=other_id)
    human_id, human_type, _membership = seed_actor(
        session, organization_id=other_id, principal_type="human", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(human_id, human_type, other_id, correlation_id="corr-org")

    book_appointment(session, ctx=ctx, start=local(9), **{k: v for k, v in ids.items() if k != "organization_id"})

    event = audit_rows(session, action="appointment.created")[0]
    assert event.organization_id == other_id
    assert event.actor_id == str(human_id)


# --- 7. cross-org context cannot mutate another tenant -----------------------


def test_cross_org_context_cannot_mutate_another_tenant(session):
    org_a = create_organization(session, "Org A").id
    org_b = create_organization(session, "Org B").id
    b_ids = seed_booking(session, organization_id=org_b)

    # ctx org = A, but every referenced resource belongs to org B: the
    # tenant-scoped reads resolve them in A -> NOT_FOUND, no mutation, no audit.
    human_id, human_type, _membership = seed_actor(
        session, organization_id=org_a, principal_type="human", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(human_id, human_type, org_a, correlation_id="corr-a")

    with pytest.raises(AppError) as exc:
        book_appointment(
            session,
            ctx=ctx,
            lead_id=b_ids["lead_id"],
            service_id=b_ids["service_id"],
            location_id=b_ids["location_id"],
            practitioner_id=b_ids["practitioner_id"],
            start=local(9),
        )
    assert exc.value.code is ErrorCode.NOT_FOUND
    session.rollback()
    assert count_of(session, Appointment) == 0
    assert len(audit_rows(session, action="appointment.created")) == 0


# --- 8-9. authorization wiring (PF0 §12, F-4/F-5) ---------------------------


def test_location_scoped_permission_remains_enforced(session):
    org = create_organization(session, "Branches").id
    loc_a = create_location(session, LocationCreate(name="Sede A", timezone=LIMA), org)
    loc_b = create_location(session, LocationCreate(name="Sede B", timezone=LIMA), org)

    principal = create_principal(session, display_name="Scoped", principal_type="human")
    membership = add_membership(session, organization_id=org, principal_id=principal.id)
    role = create_role(session, organization_id=org, code="branch-a", name="Branch A")
    grant_permission(session, role_id=role.id, permission_code=APPOINTMENTS_CREATE)
    assign_role(
        session, organization_id=org, membership_id=membership.id, role_id=role.id,
        location_id=loc_a.id,
    )

    assert has_permission(session, principal.id, org, APPOINTMENTS_CREATE, loc_a.id) is True
    assert has_permission(session, principal.id, org, APPOINTMENTS_CREATE, loc_b.id) is False
    # A location-less (org-wide) check needs an org-wide grant (E5).
    assert has_permission(session, principal.id, org, APPOINTMENTS_CREATE) is False


def test_inactive_membership_remains_denied(session):
    human_id, human_type, membership_id = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_READ,)
    )
    assert has_permission(session, human_id, ORG, APPOINTMENTS_READ) is True

    set_membership_active(session, membership_id, False)

    assert has_permission(session, human_id, ORG, APPOINTMENTS_READ) is False
    assert count_of(session, Appointment) == 0


# --- 10-12. mutations record provenance (D2/D3) ------------------------------


def _booking_kwargs(ids):
    return {
        "lead_id": ids["lead_id"],
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "practitioner_id": ids["practitioner_id"],
    }


def test_booking_audit_stores_provenance(session):
    ids = seed_booking(session)
    human_id, human_type, _membership = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(human_id, human_type, ORG, correlation_id="corr-book")

    book_appointment(session, ctx=ctx, start=local(9), **_booking_kwargs(ids))

    event = audit_rows(session, action="appointment.created")[0]
    assert event.actor_id == str(human_id)
    assert event.actor_type == "human"
    assert event.correlation_id == "corr-book"
    assert event.organization_id == ORG


def test_reschedule_audit_stores_provenance(session):
    ids = seed_booking(session)
    human_id, human_type, _membership = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_CREATE, APPOINTMENTS_RESCHEDULE)
    )
    ctx = ctx_for(human_id, human_type, ORG, correlation_id="corr-resched")
    appointment = book_appointment(session, ctx=ctx, start=local(9), **_booking_kwargs(ids))

    reschedule_appointment(session, appointment.id, local(10), ctx=ctx)

    event = audit_rows(session, action="appointment.rescheduled")[0]
    assert event.actor_id == str(human_id)
    assert event.actor_type == "human"
    assert event.correlation_id == "corr-resched"
    assert event.organization_id == ORG


def test_cancel_audit_stores_provenance(session):
    ids = seed_booking(session)
    human_id, human_type, _membership = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_CREATE, APPOINTMENTS_CANCEL)
    )
    ctx = ctx_for(human_id, human_type, ORG, correlation_id="corr-cancel")
    appointment = book_appointment(session, ctx=ctx, start=local(9), **_booking_kwargs(ids))

    cancel_appointment(session, appointment.id, ctx=ctx)

    event = audit_rows(session, action="appointment.cancelled")[0]
    assert event.actor_id == str(human_id)
    assert event.actor_type == "human"
    assert event.correlation_id == "corr-cancel"
    assert event.organization_id == ORG


# --- 13-14. audit atomicity (A3/D1) ------------------------------------------


def test_failed_mutation_writes_no_audit_event(session):
    ids = seed_booking(session)
    human_id, human_type, _membership = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(human_id, human_type, ORG)

    with pytest.raises(AppError) as exc:
        book_appointment(session, ctx=ctx, start=local(15), **_booking_kwargs(ids))
    assert exc.value.code is ErrorCode.SLOT_BLOCKED

    session.rollback()
    assert count_of(session, Appointment) == 0
    assert count_of(session, AuditEvent) == 0


def test_mutation_and_audit_remain_atomic(session, monkeypatch):
    ids = seed_booking(session)
    human_id, human_type, _membership = seed_actor(
        session, principal_type="human", codes=(APPOINTMENTS_CREATE,)
    )
    ctx = ctx_for(human_id, human_type, ORG)

    def boom(*args, **kwargs):
        raise RuntimeError("audit writer exploded")

    monkeypatch.setattr("app.scheduling.service.record_event", boom)

    with pytest.raises(RuntimeError):
        book_appointment(session, ctx=ctx, start=local(9), **_booking_kwargs(ids))

    assert count_of(session, Appointment) == 0
    assert count_of(session, AuditEvent) == 0
