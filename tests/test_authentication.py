"""HTTP authentication: the transport must prove who it is before anything else.

Before this suite, ``resolve_http_context`` returned constants — every
anonymous request became the seeded ``system`` principal holding the whole
permission catalog. The authorization layer was already complete and tested;
what was missing was the door in front of it.

These are deliberately *negative* tests. A suite that only exercises the happy
path cannot detect an authentication regression: reverting the feature would
leave it green. Each test here fails if the door is removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.db import get_db
from app.iam.credentials import (
    AUTHENTICATION_REQUIRED_HTTP_STATUS,
    IntegrationCredential,
    issue_credential,
)
from app.iam.models import SYSTEM_PRINCIPAL_ID, Membership, Principal, Role, RoleAssignment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

UTC = timezone.utc
ORG = BOOTSTRAP_ORGANIZATION_ID


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
    # Authentication resolves on its own short-lived session so the request
    # session stays idle for services that own their transaction.
    app.state.auth_sessionmaker = maker
    return app, maker


@pytest.fixture
def client(api_app):
    app, _ = api_app
    return TestClient(app, raise_server_exceptions=False)


def _grant_all(session, organization_id: int, principal_id: int, code: str) -> None:
    """Give a principal the whole catalog in one organization, via the normal path."""
    role = Role(organization_id=organization_id, code=code, name=code)
    session.add(role)
    session.flush()
    session.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT :role, p.id FROM permissions p"
        ),
        {"role": role.id},
    )
    membership = Membership(organization_id=organization_id, principal_id=principal_id)
    session.add(membership)
    session.flush()
    session.add(
        RoleAssignment(
            organization_id=organization_id, membership_id=membership.id, role_id=role.id
        )
    )
    session.flush()


def make_integration(session, *, organization_id=ORG, name="n8n", expires_at=None):
    """Create an integration principal with a credential; return the plaintext token."""
    principal = Principal(type="integration", display_name=name)
    session.add(principal)
    session.flush()
    _grant_all(session, organization_id, principal.id, f"role-{name}-{organization_id}")
    credential, token = issue_credential(
        session,
        organization_id=organization_id,
        principal_id=principal.id,
        name=name,
        expires_at=expires_at,
    )
    session.commit()
    return principal, credential, token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- the door


def test_request_without_any_credential_is_rejected(client):
    """The regression that started this: anonymous callers were superusers.

    A 422 here would mean the request reached body validation — that is, it
    passed authentication. Only 401 proves the door exists.
    """
    read = client.get("/services")
    assert read.status_code == AUTHENTICATION_REQUIRED_HTTP_STATUS, read.text

    write = client.post("/services", json={"name": "Limpieza", "duration_minutes": 30})
    assert write.status_code == AUTHENTICATION_REQUIRED_HTTP_STATUS, write.text
    assert write.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-real-token"},
        {"Authorization": "Bearer ofk_aaaaaaaa_bbbbbbbbbbbbbbbbbbbbbbbb"},
    ],
)
def test_malformed_or_unknown_credentials_are_rejected(client, header):
    response = client.get("/services", headers=header)
    assert response.status_code == AUTHENTICATION_REQUIRED_HTTP_STATUS, response.text


def test_a_valid_credential_authenticates(client, session):
    _principal, _credential, token = make_integration(session)
    response = client.get("/services", headers=auth(token))
    assert response.status_code == 200, response.text


def test_revoked_credential_is_rejected(client, session):
    _principal, credential, token = make_integration(session)
    assert client.get("/services", headers=auth(token)).status_code == 200

    credential.revoked_at = datetime.now(UTC)
    session.commit()

    assert (
        client.get("/services", headers=auth(token)).status_code
        == AUTHENTICATION_REQUIRED_HTTP_STATUS
    )


def test_deactivated_credential_is_rejected(client, session):
    _principal, credential, token = make_integration(session)
    credential.is_active = False
    session.commit()

    assert (
        client.get("/services", headers=auth(token)).status_code
        == AUTHENTICATION_REQUIRED_HTTP_STATUS
    )


def test_expired_credential_is_rejected(client, session):
    _principal, _credential, token = make_integration(
        session, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert (
        client.get("/services", headers=auth(token)).status_code
        == AUTHENTICATION_REQUIRED_HTTP_STATUS
    )


def test_inactive_principal_is_rejected(client, session):
    principal, _credential, token = make_integration(session)
    principal.is_active = False
    session.commit()

    assert (
        client.get("/services", headers=auth(token)).status_code
        == AUTHENTICATION_REQUIRED_HTTP_STATUS
    )


# ------------------------------------------------------- identity integrity


def test_declared_headers_cannot_override_the_resolved_identity(client, session):
    """Tenant and principal come from PostgreSQL, never from what the caller says."""
    _principal, credential, token = make_integration(session)
    spoofed = {
        **auth(token),
        "X-Organization-Id": "999",
        "X-Principal-Id": str(SYSTEM_PRINCIPAL_ID),
        "X-Principal-Type": "system",
    }
    created = client.post(
        "/services", json={"name": "Limpieza", "duration_minutes": 30}, headers=spoofed
    )
    assert created.status_code == 201, created.text

    row = session.execute(
        text("SELECT organization_id FROM services WHERE name = 'Limpieza'")
    ).scalar_one()
    assert row == credential.organization_id


def test_the_system_principal_is_not_reachable_over_http(client, session):
    """``system`` keeps the whole catalog, so it must never be a transport identity."""
    _principal, _credential, token = make_integration(session)
    client.post("/services", json={"name": "Limpieza", "duration_minutes": 30}, headers=auth(token))

    actors = session.execute(
        text("SELECT DISTINCT actor_id FROM audit_events")
    ).scalars().all()
    assert str(SYSTEM_PRINCIPAL_ID) not in {str(a) for a in actors}


def test_a_credential_cannot_read_another_organization(client, session):
    """The isolation proof the empty database could never give."""
    other_org = session.execute(
        text("INSERT INTO organizations (name) VALUES ('Otra Clinica') RETURNING id")
    ).scalar_one()
    session.commit()

    _p_a, cred_a, token_a = make_integration(session, name="int-a")
    _p_b, _cred_b, token_b = make_integration(session, organization_id=other_org, name="int-b")

    created = client.post(
        "/services", json={"name": "Solo de A", "duration_minutes": 30}, headers=auth(token_a)
    )
    assert created.status_code == 201, created.text
    assert cred_a.organization_id == ORG

    seen_by_b = client.get("/services", headers=auth(token_b))
    assert seen_by_b.status_code == 200
    assert [s["name"] for s in seen_by_b.json()] == []


# ----------------------------------------------------------- no leakage


def test_the_rejection_does_not_reveal_whether_the_key_existed(client, session):
    _principal, credential, token = make_integration(session)
    credential.revoked_at = datetime.now(UTC)
    session.commit()

    revoked = client.get("/services", headers=auth(token))
    unknown = client.get("/services", headers=auth("ofk_zzzzzzzz_qqqqqqqqqqqqqqqqqqqqqqqq"))

    assert revoked.status_code == unknown.status_code
    assert revoked.json() == unknown.json()


def test_the_secret_is_never_stored_in_clear(client, session):
    _principal, credential, token = make_integration(session)
    secret = token.rsplit("_", 1)[-1]

    stored = session.scalar(
        select(IntegrationCredential).where(IntegrationCredential.id == credential.id)
    )
    assert secret not in stored.secret_hash
    assert stored.secret_hash != secret
    assert len(stored.secret_hash) == 64  # sha256 hex

    # And nothing in the row carries the plaintext.
    row = session.execute(
        text("SELECT * FROM integration_credentials WHERE id = :i"), {"i": credential.id}
    ).mappings().one()
    assert secret not in " ".join(str(v) for v in row.values())


def test_a_failed_authentication_is_audited_without_the_secret(client, session):
    client.get("/services", headers=auth("ofk_deadbeef_supersecretvalue123456"))

    rows = session.execute(text("SELECT * FROM audit_events")).mappings().all()
    dumped = " ".join(str(dict(r)) for r in rows)
    assert "supersecretvalue123456" not in dumped
