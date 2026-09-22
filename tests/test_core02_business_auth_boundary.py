"""CORE-02 — the Lead-to-Appointment / Reception-Scheduling business routes
must reject a missing or invalid credential with 401, even while
``ERP_ANONYMOUS_COMPAT`` stays enabled for the legacy surfaces it exists for.

Before this suite, ``resolve_http_context`` treated ``ERP_ANONYMOUS_COMPAT`` as
a blanket amnesty: every one of these 27 routes silently ran as the seeded
``system`` principal (the whole permission catalog, in the bootstrap
organization) whenever no credential was presented. An anonymous caller with
network access to the API was therefore a superuser over leads, patients,
visits, executions, availability and appointments. These tests are
deliberately negative: each one fails if that door is reopened.

The four appointment-proposal routes (CORE-01) already authenticated directly
and are proven elsewhere (``tests/test_appointment_proposals.py``); this suite
covers exactly the 27 routes that previously fell back to the compatibility
identity.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.db import get_db
from app.iam.credentials import AUTHENTICATION_REQUIRED_HTTP_STATUS, issue_credential
from app.iam.models import Membership, Principal, Role, RoleAssignment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

ORG = BOOTSTRAP_ORGANIZATION_ID


def _app_for(migrated_engine):
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
    return app


def _token_with_all_permissions(session, *, organization_id: int, name: str) -> str:
    principal = Principal(type="integration", display_name=name)
    session.add(principal)
    session.flush()
    membership = Membership(organization_id=organization_id, principal_id=principal.id)
    role = Role(organization_id=organization_id, code=name, name=name)
    session.add_all((membership, role))
    session.flush()
    session.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT :role, id FROM permissions"
        ),
        {"role": role.id},
    )
    session.add(
        RoleAssignment(
            organization_id=organization_id,
            membership_id=membership.id,
            role_id=role.id,
        )
    )
    session.flush()
    _credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=name,
    )
    session.commit()
    return token


#: The 27 routes that resolved through the ``ERP_ANONYMOUS_COMPAT`` fallback
#: (docs/handoffs/plans/2026-09-22-core-02-route-matrix.yaml). Every payload is
#: schema-valid but references non-existent ids where a foreign key is
#: required: authentication must reject the call before any of that is ever
#: looked at, so the exact domain outcome behind the gate does not matter here.
PROTECTED_ROUTES = [
    ("GET", "/leads", None, None),
    ("POST", "/leads", None, {"full_name": "Gate Lead", "contact_phone": "+51900000000", "acquisition_source": "direct"}),
    ("GET", "/leads/999999", None, None),
    ("POST", "/services", None, {"name": "Gate Service", "duration_minutes": 30}),
    ("GET", "/services", None, None),
    ("GET", "/locations", None, None),
    ("POST", "/locations", None, {"name": "Gate Location", "timezone": "America/Lima"}),
    ("POST", "/practitioners", None, {"display_name": "Gate Practitioner"}),
    (
        "POST",
        "/capabilities",
        None,
        {"practitioner_id": 999999, "service_id": 999999, "location_id": 999999},
    ),
    (
        "GET",
        "/practitioners/eligible",
        {"service_id": 999999, "location_id": 999999},
        None,
    ),
    ("POST", "/patients", None, {"full_name": "Gate Patient"}),
    ("GET", "/patients", None, None),
    ("GET", "/patients/999999", None, None),
    (
        "POST",
        "/visits",
        None,
        {"patient_id": 999999, "practitioner_id": 999999, "location_id": 999999},
    ),
    ("GET", "/visits", None, None),
    ("GET", "/visits/999999", None, None),
    ("GET", "/visits/999999/executions", None, None),
    ("GET", "/executions", None, None),
    (
        "POST",
        "/visits/999999/executions",
        None,
        {"service_id": 999999, "executed_price": "10.00"},
    ),
    (
        "POST",
        "/availability-rules",
        None,
        {
            "practitioner_id": 999999,
            "location_id": 999999,
            "day_of_week": 1,
            "start_local": "09:00:00",
            "end_local": "10:00:00",
        },
    ),
    (
        "POST",
        "/schedule-blocks",
        None,
        {
            "practitioner_id": 999999,
            "location_id": 999999,
            "start_utc": "2026-09-23T00:00:00Z",
            "end_utc": "2026-09-23T01:00:00Z",
        },
    ),
    (
        "POST",
        "/slots/query",
        None,
        {
            "service_id": 999999,
            "location_id": 999999,
            "window_start": "2026-09-23T00:00:00Z",
            "window_end": "2026-09-24T00:00:00Z",
        },
    ),
    ("GET", "/appointments", None, None),
    ("GET", "/appointments/999999", None, None),
    (
        "POST",
        "/appointments",
        None,
        {
            "lead_id": 999999,
            "service_id": 999999,
            "location_id": 999999,
            "practitioner_id": 999999,
            "start": "2026-09-23T00:00:00Z",
        },
    ),
    ("POST", "/appointments/999999/cancel", None, {}),
    ("POST", "/appointments/999999/reschedule", None, {"new_start": "2026-09-23T00:00:00Z"}),
]

ROUTE_IDS = [f"{method} {path}" for method, path, _, _ in PROTECTED_ROUTES]

assert len(PROTECTED_ROUTES) == 27, "the CORE-02 route matrix declares exactly 27 routes"


@pytest.mark.parametrize("method,path,query,json_body", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_protected_business_route_rejects_missing_credential_even_with_compat_enabled(
    monkeypatch, migrated_engine, method, path, query, json_body
):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ERP_ANONYMOUS_COMPAT", "true")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.request(method, path, params=query, json=json_body)

    assert response.status_code == AUTHENTICATION_REQUIRED_HTTP_STATUS, (
        method,
        path,
        response.text,
    )
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize("method,path,query,json_body", PROTECTED_ROUTES, ids=ROUTE_IDS)
def test_protected_business_route_rejects_an_invalid_credential_even_with_compat_enabled(
    monkeypatch, migrated_engine, method, path, query, json_body
):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ERP_ANONYMOUS_COMPAT", "true")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.request(
            method,
            path,
            params=query,
            json=json_body,
            headers={"Authorization": "Bearer ofk_notreal_bbbbbbbbbbbbbbbbbbbbbbbb"},
        )

    assert response.status_code == AUTHENTICATION_REQUIRED_HTTP_STATUS, (
        method,
        path,
        response.text,
    )
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_authenticated_caller_passes_the_gate_across_the_protected_business_surface(
    migrated_engine,
):
    """The gate must not over-block: a real credential with full permissions
    still reaches every one of these 27 routes and gets a real domain
    response, exercised here end to end with referentially valid data.
    """
    with TestClient(
        _app_for(migrated_engine), raise_server_exceptions=False, headers=AUTH_HEADERS
    ) as client:
        lead = client.post(
            "/leads",
            json={
                "full_name": "Auth Gate Lead",
                "contact_phone": "+51900000001",
                "acquisition_source": "direct",
            },
        )
        assert lead.status_code == 201, lead.text
        assert client.get("/leads").status_code == 200
        assert client.get(f"/leads/{lead.json()['id']}").status_code == 200

        service = client.post(
            "/services", json={"name": "Auth Gate Service", "duration_minutes": 30}
        )
        assert service.status_code == 201, service.text
        assert client.get("/services").status_code == 200

        location = client.post(
            "/locations", json={"name": "Auth Gate Location", "timezone": "America/Lima"}
        )
        assert location.status_code == 201, location.text
        assert client.get("/locations").status_code == 200

        practitioner = client.post(
            "/practitioners", json={"display_name": "Auth Gate Practitioner"}
        )
        assert practitioner.status_code == 201, practitioner.text

        capability = client.post(
            "/capabilities",
            json={
                "practitioner_id": practitioner.json()["id"],
                "service_id": service.json()["id"],
                "location_id": location.json()["id"],
            },
        )
        assert capability.status_code == 201, capability.text

        eligible = client.get(
            "/practitioners/eligible",
            params={"service_id": service.json()["id"], "location_id": location.json()["id"]},
        )
        assert eligible.status_code == 200, eligible.text

        patient = client.post("/patients", json={"full_name": "Auth Gate Patient"})
        assert patient.status_code == 201, patient.text
        assert client.get("/patients").status_code == 200
        assert client.get(f"/patients/{patient.json()['id']}").status_code == 200

        visit = client.post(
            "/visits",
            json={
                "patient_id": patient.json()["id"],
                "practitioner_id": practitioner.json()["id"],
                "location_id": location.json()["id"],
            },
        )
        assert visit.status_code == 201, visit.text
        assert client.get("/visits").status_code == 200
        assert client.get(f"/visits/{visit.json()['id']}").status_code == 200
        assert client.get(f"/visits/{visit.json()['id']}/executions").status_code == 200
        assert client.get("/executions").status_code == 200

        execution = client.post(
            f"/visits/{visit.json()['id']}/executions",
            json={"service_id": service.json()["id"], "executed_price": "10.00"},
        )
        assert execution.status_code == 201, execution.text

        rule = client.post(
            "/availability-rules",
            json={
                "practitioner_id": practitioner.json()["id"],
                "location_id": location.json()["id"],
                "day_of_week": 1,
                "start_local": "09:00:00",
                "end_local": "17:00:00",
            },
        )
        assert rule.status_code == 201, rule.text

        block = client.post(
            "/schedule-blocks",
            json={
                "practitioner_id": practitioner.json()["id"],
                "location_id": location.json()["id"],
                "start_utc": "2026-09-23T20:00:00Z",
                "end_utc": "2026-09-23T21:00:00Z",
            },
        )
        assert block.status_code == 201, block.text

        slots = client.post(
            "/slots/query",
            json={
                "service_id": service.json()["id"],
                "location_id": location.json()["id"],
                "window_start": "2026-09-23T00:00:00Z",
                "window_end": "2026-09-24T00:00:00Z",
            },
        )
        assert slots.status_code == 200, slots.text

        assert client.get("/appointments").status_code == 200
        assert client.get("/appointments/999999").status_code == 404

        appointment = client.post(
            "/appointments",
            json={
                "lead_id": lead.json()["id"],
                "service_id": service.json()["id"],
                "location_id": location.json()["id"],
                "practitioner_id": practitioner.json()["id"],
                "start": "2026-09-23T00:00:00Z",
            },
        )
        # The gate only has to let an authenticated, permitted caller reach the
        # domain layer; whether this particular instant is bookable is a
        # scheduling-domain concern proven by ``test_lead_to_appointment_e2e.py``.
        assert appointment.status_code not in (401, 403), appointment.text

        assert client.post("/appointments/999999/cancel").status_code == 404
        assert (
            client.post(
                "/appointments/999999/reschedule",
                json={"new_start": "2026-09-23T00:00:00Z"},
            ).status_code
            == 404
        )


def test_cross_organization_credential_is_denied_on_protected_business_reads(
    migrated_engine, session
):
    """Tenant isolation must still hold once these routes are actually gated."""
    other_org = session.execute(
        text("INSERT INTO organizations (name) VALUES ('CORE-02 Other Org') RETURNING id")
    ).scalar_one()
    session.commit()
    other_token = _token_with_all_permissions(
        session, organization_id=other_org, name="core02-other-org"
    )

    with TestClient(
        _app_for(migrated_engine), raise_server_exceptions=False, headers=AUTH_HEADERS
    ) as client:
        lead = client.post(
            "/leads",
            json={
                "full_name": "Tenant Isolation Lead",
                "contact_phone": "+51900000002",
                "acquisition_source": "direct",
            },
        )
        assert lead.status_code == 201, lead.text
        patient = client.post("/patients", json={"full_name": "Tenant Isolation Patient"})
        assert patient.status_code == 201, patient.text

        other_headers = {"Authorization": f"Bearer {other_token}"}
        lead_read = client.get(f"/leads/{lead.json()['id']}", headers=other_headers)
        patient_read = client.get(f"/patients/{patient.json()['id']}", headers=other_headers)

    assert lead_read.status_code == 404, lead_read.text
    assert lead_read.json()["error"]["code"] == "NOT_FOUND"
    assert patient_read.status_code == 404, patient_read.text
    assert patient_read.json()["error"]["code"] == "NOT_FOUND"


def test_business_route_authentication_failure_is_redacted_and_audited(
    migrated_engine, session
):
    """The shared authentication path — not a duplicated check — guards these
    routes too: a rejected credential is never stored or echoed in the clear.
    """
    secret = "supersecretvalue123456"

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.get(
            "/leads", headers={"Authorization": f"Bearer ofk_deadbeef_{secret}"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    event = session.execute(
        text(
            "SELECT event_type, outcome, organization_id, principal_id, metadata "
            "FROM security_events ORDER BY id DESC LIMIT 1"
        )
    ).mappings().one()
    assert event["event_type"] == "authentication"
    assert event["outcome"] == "failed"
    assert event["organization_id"] is None
    assert event["principal_id"] is None
    assert secret not in str(dict(event))
