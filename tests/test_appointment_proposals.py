"""CORE-01 SubCard A: HTTP surface for human review of AIRY appointment proposals.

Routes under test do not exist yet — these tests are intentionally red until
the router lands. Fixture/setup patterns are copied from
``test_agent_booking_phase4.py`` and ``test_reception_agent_phase5.py``.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.iam.credentials import issue_credential
from app.iam.models import Membership, Permission, Role, RoleAssignment, RolePermission
from app.iam.permissions import APPOINTMENTS_CREATE, APPOINTMENTS_READ
from app.messaging.models import ChannelAccount, ContactIdentity, Conversation
from app.organization.models import (
    Location,
    Organization,
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


def _seed_tenant(session, *, organization_id: int, suffix: str, phone: str):
    service = Service(
        organization_id=organization_id,
        name=f"Limpieza {suffix}",
        duration_minutes=60,
        is_active=True,
    )
    location = Location(
        organization_id=organization_id,
        name=f"Sede {suffix}",
        timezone="America/Lima",
        is_active=True,
    )
    practitioner = Practitioner(display_name=f"Dra. {suffix}", is_active=True)
    channel = ChannelAccount(
        organization_id=organization_id,
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
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            is_active=True,
        )
    )
    session.flush()
    session.add_all(
        [
            PractitionerCapability(
                organization_id=organization_id,
                practitioner_id=practitioner.id,
                service_id=service.id,
                location_id=location.id,
                is_active=True,
            ),
            AvailabilityRule(
                organization_id=organization_id,
                practitioner_id=practitioner.id,
                location_id=location.id,
                day_of_week=0,
                start_local=time(9),
                end_local=time(12),
            ),
        ]
    )
    contact = ContactIdentity(
        organization_id=organization_id,
        channel_account_id=channel.id,
        external_contact_id=f"contact-{suffix}",
        normalized_phone_e164=phone,
        consent_status="opted_in",
    )
    session.add(contact)
    session.flush()
    conversation = Conversation(
        organization_id=organization_id,
        channel_account_id=channel.id,
        contact_identity_id=contact.id,
        status="open",
        last_message_at=datetime(2026, 8, 23, 15, tzinfo=UTC),
    )
    lead = Lead(
        organization_id=organization_id,
        full_name=f"Paciente {suffix}",
        contact_phone=phone,
        acquisition_source="direct",
    )
    session.add_all([conversation, lead])
    session.commit()
    return {
        "organization_id": organization_id,
        "service": service,
        "location": location,
        "practitioner": practitioner,
        "contact": contact,
        "conversation": conversation,
        "lead": lead,
    }


def _seed_proposal(session, seeded, *, start_utc: datetime, status: str = "pending", **overrides):
    proposal = AppointmentProposal(
        organization_id=seeded["organization_id"],
        conversation_id=seeded["conversation"].id,
        contact_identity_id=seeded["contact"].id,
        lead_id=overrides.pop("lead_id", seeded["lead"].id),
        patient_id=overrides.pop("patient_id", None),
        service_id=seeded["service"].id,
        practitioner_id=seeded["practitioner"].id,
        location_id=seeded["location"].id,
        full_name="Paciente Prueba",
        start_utc=start_utc,
        end_utc=start_utc + timedelta(hours=1),
        confirmation_token=uuid4(),
        status=status,
        expires_at=overrides.pop("expires_at", datetime.now(UTC) + timedelta(minutes=15)),
        **overrides,
    )
    session.add(proposal)
    session.commit()
    return proposal


def _other_organization(session, *, suffix: str) -> int:
    org = Organization(name=f"Otra Clinica {suffix}")
    session.add(org)
    session.commit()
    return org.id


def _location_scoped_headers(session, *, organization_id: int, location_id: int, suffix: str):
    """A human principal whose only role grant is narrowed to ``location_id``."""
    from app.iam.models import Principal

    principal = Principal(type="human", display_name=f"Recepcion {suffix}")
    session.add(principal)
    session.flush()
    membership = Membership(organization_id=organization_id, principal_id=principal.id)
    session.add(membership)
    session.flush()
    role = Role(organization_id=organization_id, code=f"loc-scoped-{suffix}", name=f"loc-scoped-{suffix}")
    session.add(role)
    session.flush()
    permission_ids = session.scalars(
        select(Permission.id).where(Permission.code.in_([APPOINTMENTS_READ, APPOINTMENTS_CREATE]))
    ).all()
    session.add_all(RolePermission(role_id=role.id, permission_id=pid) for pid in permission_ids)
    session.add(
        RoleAssignment(
            organization_id=organization_id,
            membership_id=membership.id,
            role_id=role.id,
            location_id=location_id,
        )
    )
    session.commit()
    _credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=f"loc-scoped-{suffix}",
    )
    session.commit()
    return {"Authorization": f"Bearer {token}"}


def _agent_headers(session, *, organization_id: int, suffix: str):
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=organization_id,
        name=f"agent-{suffix}",
        principal_type="agent",
    )
    _assign_profile(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        profile="sales-agent-v0",
    )
    _credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=f"agent-{suffix}",
    )
    session.commit()
    return {"Authorization": f"Bearer {token}"}


def test_human_lists_own_tenant_proposals_only(client, session):
    own = _seed_tenant(session, organization_id=ORG, suffix="list-own", phone="+51999130001")
    other_org_id = _other_organization(session, suffix="list-own")
    other = _seed_tenant(
        session, organization_id=other_org_id, suffix="list-other", phone="+51999130002"
    )
    own_proposal = _seed_proposal(
        session, own, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )
    _seed_proposal(session, other, start_utc=datetime(2026, 8, 31, 15, tzinfo=UTC))

    response = client.get("/scheduling/appointment-proposals")

    assert response.status_code == 200, response.text
    ids = [row["id"] for row in response.json()]
    assert own_proposal.id in ids
    assert all(row["id"] != own_proposal.id or row is not None for row in response.json())
    assert len(ids) == 1


def test_cross_tenant_read_is_not_disclosed(client, session):
    own = _seed_tenant(session, organization_id=ORG, suffix="read-own", phone="+51999130003")
    other_org_id = _other_organization(session, suffix="read-own")
    other = _seed_tenant(
        session, organization_id=other_org_id, suffix="read-other", phone="+51999130004"
    )
    foreign_proposal = _seed_proposal(
        session, other, start_utc=datetime(2026, 8, 31, 16, tzinfo=UTC)
    )

    response = client.get(f"/scheduling/appointment-proposals/{foreign_proposal.id}")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_cross_location_read_and_confirm_denied(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="cross-loc", phone="+51999130005")
    other_location = Location(
        organization_id=ORG,
        name="Sede Ajena",
        timezone="America/Lima",
        is_active=True,
    )
    session.add(other_location)
    session.commit()
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 17, tzinfo=UTC)
    )
    scoped_headers = _location_scoped_headers(
        session, organization_id=ORG, location_id=other_location.id, suffix="cross-loc"
    )

    read_response = client.get(
        f"/scheduling/appointment-proposals/{proposal.id}", headers=scoped_headers
    )
    confirm_response = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={**scoped_headers, "Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    assert read_response.status_code == 403, read_response.text
    assert read_response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert confirm_response.status_code == 403, confirm_response.text
    assert confirm_response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0


def test_human_confirm_books_exactly_one_appointment_with_one_audit_and_receipt(
    client, session
):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="confirm", phone="+51999130006")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )

    response = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["appointment_id"] is not None
    assert body["status"] == "confirmed"

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1
    stored = session.get(AppointmentProposal, proposal.id)
    assert stored.status == "confirmed"
    assert stored.appointment_id is not None
    audit_count = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "appointment_proposal.confirmed")
    )
    assert audit_count == 1


def test_same_key_confirm_replay_returns_same_result_without_duplication(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="replay", phone="+51999130007")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )
    key = str(uuid4())
    body = {
        "conversation_id": seeded["conversation"].id,
        "confirmation_token": str(proposal.confirmation_token),
    }

    first = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={"Idempotency-Key": key},
        json=body,
    )
    replay = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={"Idempotency-Key": key},
        json=body,
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1
    audit_count = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "appointment_proposal.confirmed")
    )
    assert audit_count == 1


def test_two_concurrent_confirms_produce_exactly_one_winner(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="concurrent", phone="+51999130008")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )
    body = {
        "conversation_id": seeded["conversation"].id,
        "confirmation_token": str(proposal.confirmation_token),
    }
    results: list = [None, None]

    def _call(index: int):
        results[index] = client.post(
            "/scheduling/appointment-proposals/confirm",
            headers={"Idempotency-Key": str(uuid4())},
            json=body,
        )

    threads = [threading.Thread(target=_call, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = [response.status_code for response in results]
    assert statuses.count(200) == 1
    assert 409 in statuses or 200 in statuses
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1


def test_expired_proposal_confirm_fails_closed(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="expired", phone="+51999130009")
    proposal = _seed_proposal(
        session,
        seeded,
        start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    response = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    assert session.get(AppointmentProposal, proposal.id).status != "confirmed"


def test_slot_conflict_confirm_fails_closed(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="conflict", phone="+51999130010")
    start = datetime(2026, 8, 31, 14, tzinfo=UTC)
    conflicting = Appointment(
        organization_id=ORG,
        lead_id=seeded["lead"].id,
        patient_id=None,
        service_id=seeded["service"].id,
        practitioner_id=seeded["practitioner"].id,
        location_id=seeded["location"].id,
        start_utc=start,
        end_utc=start + timedelta(hours=1),
        state="confirmed",
    )
    session.add(conflicting)
    session.commit()
    proposal = _seed_proposal(session, seeded, start_utc=start)

    response = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SLOT_BLOCKED"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 1
    assert session.get(AppointmentProposal, proposal.id).status != "confirmed"


def test_human_decline_expires_proposal_and_reopens_conversation(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="decline", phone="+51999130011")
    seeded["conversation"].status = "awaiting_confirmation"
    session.commit()
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )

    response = client.post(
        "/scheduling/appointment-proposals/decline",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    assert response.status_code == 200, response.text
    session.expire_all()
    stored = session.get(AppointmentProposal, proposal.id)
    assert stored.status == "expired"
    assert stored.appointment_id is None
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    audit_count = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "appointment_proposal.declined")
    )
    assert audit_count == 1
    conversation = session.get(Conversation, seeded["conversation"].id)
    assert conversation.status == "open"


def test_repeat_decline_is_idempotent_without_duplicate_audit(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="redecline", phone="+51999130012")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )
    body = {
        "conversation_id": seeded["conversation"].id,
        "confirmation_token": str(proposal.confirmation_token),
    }

    first = client.post(
        "/scheduling/appointment-proposals/decline",
        headers={"Idempotency-Key": str(uuid4())},
        json=body,
    )
    second = client.post(
        "/scheduling/appointment-proposals/decline",
        headers={"Idempotency-Key": str(uuid4())},
        json=body,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    session.expire_all()
    audit_count = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "appointment_proposal.declined")
    )
    assert audit_count == 1


def test_declining_an_already_confirmed_proposal_is_refused(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="already", phone="+51999130013")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC), status="confirmed"
    )

    response = client.post(
        "/scheduling/appointment-proposals/decline",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    session.expire_all()
    assert session.get(AppointmentProposal, proposal.id).status == "confirmed"


def test_agent_principal_confirm_is_refused_before_any_replay_return(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="agent-deny", phone="+51999130014")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )
    agent_headers = _agent_headers(session, organization_id=ORG, suffix="agent-deny")
    key = str(uuid4())
    body = {
        "conversation_id": seeded["conversation"].id,
        "confirmation_token": str(proposal.confirmation_token),
    }

    first = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={**agent_headers, "Idempotency-Key": key},
        json=body,
    )
    replay = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={**agent_headers, "Idempotency-Key": key},
        json=body,
    )

    for response in (first, replay):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "INVALID_INPUT", response.text
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    assert session.get(AppointmentProposal, proposal.id).status == "pending"


def test_unauthenticated_caller_is_refused_on_all_proposal_routes(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="anon", phone="+51999130015")
    proposal = _seed_proposal(
        session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC)
    )
    # A fresh client on the same app/db wiring, deliberately without the
    # fixture's default ``AUTH_HEADERS`` — these four routes must refuse an
    # anonymous caller outright (amendment 2), never fall through to the
    # ERP_ANONYMOUS_COMPAT seeded ``system`` principal.
    anon = TestClient(client.app, raise_server_exceptions=False)

    list_response = anon.get("/scheduling/appointment-proposals")
    read_response = anon.get(f"/scheduling/appointment-proposals/{proposal.id}")
    confirm_response = anon.post(
        "/scheduling/appointment-proposals/confirm",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )
    decline_response = anon.post(
        "/scheduling/appointment-proposals/decline",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )

    for response in (list_response, read_response, confirm_response, decline_response):
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED", response.text

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    assert session.get(AppointmentProposal, proposal.id).status == "pending"
