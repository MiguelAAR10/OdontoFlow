"""IDN — human identity per person, secretaria/administrador profiles, GET /me.

Spec: ``docs/superpowers/specs/2026-10-01-erp-idn.md``.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.db import get_db
from app.economics.models import Payment
from app.iam.credentials import IntegrationCredential, issue_credential, revoke_credential
from app.iam.models import Membership, Principal, Role, RoleAssignment
from app.iam.service import add_membership
from app.organization.models import Organization
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts import issue_credential as cli
from scripts.issue_credential import (
    HUMAN_PROFILE_PERMISSIONS,
    PROFILE_PERMISSIONS,
    _assign_profile,
    _resolve_principal,
)
from test_appointment_proposals import _seed_proposal, _seed_tenant
from test_domain_gaps_helpers import (
    make_charge,
    make_location,
    make_product,
    pay,
    seed_booking,
    stock,
)

SECRETARIA_MUST = {
    "contact_appointments.book",
    "conversations.read",
    "appointments.create",
    "payments.manage",
    "follow_ups.manage",
}
ADMIN_ONLY = {
    "payments.reverse",
    "products.read",
    "products.create",
    "movements.read",
    "movements.create",
    "reorder_points.manage",
}


@pytest.fixture
def maker(migrated_engine):
    return sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client(maker):
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


def _credential(session, *, name, principal_type, profile, organization_id=ORG):
    principal = _resolve_principal(
        session, organization_id=organization_id, name=name, principal_type=principal_type
    )
    _assign_profile(
        session, organization_id=organization_id, principal_id=principal.id, profile=profile
    )
    credential, token = issue_credential(
        session, organization_id=organization_id, principal_id=principal.id, name=name
    )
    session.commit()
    return principal.id, credential.id, {"Authorization": f"Bearer {token}"}


def _lucia(session, organization_id=ORG):
    return _credential(
        session,
        name="Lucía Ramos",
        principal_type="human",
        profile="secretaria",
        organization_id=organization_id,
    )


def _carlos(session, organization_id=ORG):
    return _credential(
        session,
        name="Carlos Vega",
        principal_type="human",
        profile="administrador",
        organization_id=organization_id,
    )


def _staff(session):
    return _credential(
        session,
        name="reception-staff-demo",
        principal_type="integration",
        profile="reception-staff-demo",
    )


def _agent(session):
    return _credential(
        session, name="agent-idn", principal_type="agent", profile="sales-agent-v0"
    )


def _other_org(session, name="Clinica B") -> int:
    org = Organization(name=name)
    session.add(org)
    session.commit()
    return org.id


def _key():
    return {"Idempotency-Key": str(uuid4())}


# --- profiles ---------------------------------------------------------------


def test_human_profiles_are_explicit_and_disjoint_from_integration_profiles():
    secretaria = set(HUMAN_PROFILE_PERMISSIONS["secretaria"])
    administrador = set(HUMAN_PROFILE_PERMISSIONS["administrador"])
    assert set(HUMAN_PROFILE_PERMISSIONS) == {"secretaria", "administrador"}
    assert not set(HUMAN_PROFILE_PERMISSIONS) & set(PROFILE_PERMISSIONS)
    assert SECRETARIA_MUST <= secretaria
    assert not ADMIN_ONLY & secretaria
    assert administrador == secretaria | ADMIN_ONLY


# --- GET /me ----------------------------------------------------------------


def test_me_requires_a_credential(client, session):
    anonymous = client.get("/me")
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    _pid, credential_id, headers = _lucia(session)
    revoke_credential(session, credential_id)
    session.commit()
    revoked = client.get("/me", headers=headers)
    assert revoked.status_code == 401, revoked.text
    assert revoked.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_me_describes_each_human_profile(client, session):
    lucia_id, _cid, lucia = _lucia(session)
    carlos_id, _cid, carlos = _carlos(session)

    body = client.get("/me", headers=lucia).json()
    assert body["principal"] == {"id": lucia_id, "type": "human", "display_name": "Lucía Ramos"}
    assert body["organization"]["id"] == ORG
    assert body["roles"] == [{"code": "staff-secretaria", "name": "Secretaria"}]
    lucia_permissions = set(body["permissions"])
    assert body["permissions"] == sorted(body["permissions"])
    assert SECRETARIA_MUST <= lucia_permissions
    assert not ADMIN_ONLY & lucia_permissions

    body = client.get("/me", headers=carlos).json()
    assert body["principal"] == {"id": carlos_id, "type": "human", "display_name": "Carlos Vega"}
    assert body["roles"] == [{"code": "staff-administrador", "name": "Administrador"}]
    assert set(body["permissions"]) >= lucia_permissions | ADMIN_ONLY


def test_me_for_the_integration_staff_credential_is_unchanged(client, session):
    _pid, _cid, staff = _staff(session)

    body = client.get("/me", headers=staff).json()

    assert body["principal"]["type"] == "integration"
    assert body["roles"] == [
        {"code": "integration-reception-staff-demo", "name": "Integration: reception-staff-demo"}
    ]
    assert set(body["permissions"]) == set(PROFILE_PERMISSIONS["reception-staff-demo"])


def test_me_with_an_inactive_membership_has_no_roles_or_permissions(client, session):
    lucia_id, _cid, lucia = _lucia(session)
    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == ORG, Membership.principal_id == lucia_id
        )
    )
    membership.is_active = False
    session.commit()

    response = client.get("/me", headers=lucia)

    assert response.status_code == 200, response.text
    assert response.json()["roles"] == []
    assert response.json()["permissions"] == []


def test_me_shows_only_the_credential_organization(client, session):
    org_b = _other_org(session)
    principal = Principal(type="human", display_name="Ana Doble")
    session.add(principal)
    session.flush()
    add_membership(session, organization_id=ORG, principal_id=principal.id)
    add_membership(session, organization_id=org_b, principal_id=principal.id)
    _assign_profile(session, organization_id=ORG, principal_id=principal.id, profile="administrador")
    _assign_profile(session, organization_id=org_b, principal_id=principal.id, profile="secretaria")
    _cred, token = issue_credential(
        session, organization_id=org_b, principal_id=principal.id, name="ana-b"
    )
    session.commit()

    body = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()

    assert body["organization"] == {"id": org_b, "name": "Clinica B"}
    assert body["roles"] == [{"code": "staff-secretaria", "name": "Secretaria"}]
    assert not ADMIN_ONLY & set(body["permissions"])


# --- approve with identity --------------------------------------------------


def test_a_human_confirms_a_proposal_and_the_audit_names_her(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="idn", phone="+51999140001")
    proposal = _seed_proposal(session, seeded, start_utc=datetime(2026, 8, 31, 14, tzinfo=UTC))
    body = {
        "conversation_id": seeded["conversation"].id,
        "confirmation_token": str(proposal.confirmation_token),
    }
    lucia_id, _cid, lucia = _lucia(session)
    _sid, _cid, staff = _staff(session)
    _aid, _cid, agent = _agent(session)

    for refused in (staff, agent):
        response = client.post(
            "/scheduling/appointment-proposals/confirm",
            headers={**refused, **_key()},
            json=body,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "INVALID_INPUT"

    response = client.post(
        "/scheduling/appointment-proposals/confirm", headers={**lucia, **_key()}, json=body
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    session.expire_all()
    audit = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "appointment_proposal.confirmed")
    ).all()
    assert [(row.actor_id, row.actor_type) for row in audit] == [(str(lucia_id), "human")]


def test_only_the_administrador_reverses_a_payment(client, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    _lid, _cid, lucia = _lucia(session)
    _cid2, _c, carlos = _carlos(session)
    _sid, _c, staff = _staff(session)
    _aid, _c, agent = _agent(session)
    url = f"/payments/{payment_id}/reverse"
    body = {"reason": "Pago duplicado"}

    denied = client.post(url, headers={**lucia, **_key()}, json=body)
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    for refused in (staff, agent):
        response = client.post(url, headers={**refused, **_key()}, json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "INVALID_INPUT"

    response = client.post(url, headers={**carlos, **_key()}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["reversed"] is True


def test_only_the_administrador_transfers_stock(client, session):
    product_id = make_product(session)
    origin = make_location(session, name="Sede Origen")
    destination = make_location(session, name="Sede Destino")
    stock(session, product_id, origin, "10.00")
    _lid, _cid, lucia = _lucia(session)
    _cid2, _c, carlos = _carlos(session)
    body = {
        "origin_location_id": origin,
        "destination_location_id": destination,
        "quantity": "2.00",
    }

    denied = client.post(f"/products/{product_id}/transfers", headers={**lucia, **_key()}, json=body)
    assert denied.status_code == 403, denied.text
    allowed = client.post(
        f"/products/{product_id}/transfers", headers={**carlos, **_key()}, json=body
    )
    assert allowed.status_code == 201, allowed.text


def test_a_human_in_another_org_cannot_touch_this_org(client, session):
    seeded = _seed_tenant(session, organization_id=ORG, suffix="idn-a", phone="+51999140002")
    proposal = _seed_proposal(session, seeded, start_utc=datetime(2026, 8, 31, 15, tzinfo=UTC))
    ids = seed_booking(session, suffix="idn-pay")
    payment_id = pay(session, make_charge(session, ids), "150.00")
    org_b = _other_org(session)
    carlos_b_id, _cid, carlos_b = _carlos(session, organization_id=org_b)

    me = client.get("/me", headers=carlos_b).json()
    assert me["organization"]["id"] == org_b
    reverse = client.post(
        f"/payments/{payment_id}/reverse", headers={**carlos_b, **_key()}, json={"reason": "x"}
    )
    assert reverse.status_code == 404, reverse.text
    confirm = client.post(
        "/scheduling/appointment-proposals/confirm",
        headers={**carlos_b, **_key()},
        json={
            "conversation_id": seeded["conversation"].id,
            "confirmation_token": str(proposal.confirmation_token),
        },
    )
    assert confirm.status_code == 404, confirm.text


def test_a_human_mutation_with_a_key_replays_without_duplicating(client, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    _lid, _cid, lucia = _lucia(session)
    headers = {**lucia, **_key()}
    body = {"amount": "50.00", "method": "efectivo"}

    first = client.post(f"/charges/{charge_id}/payments", headers=headers, json=body)
    second = client.post(f"/charges/{charge_id}/payments", headers=headers, json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    session.expire_all()
    assert session.scalar(
        select(func.count()).select_from(Payment).where(Payment.charge_id == charge_id)
    ) == 1
    assert session.scalar(
        select(func.sum(Payment.amount)).where(Payment.charge_id == charge_id)
    ) == Decimal("50.00")


# --- CLI --------------------------------------------------------------------


def _run_cli(monkeypatch, maker, argv) -> int:
    monkeypatch.setattr(cli, "SessionLocal", maker)
    try:
        return cli.cmd_issue(cli.build_parser().parse_args(argv))
    except SystemExit as exc:  # argparse rejects unknown choices with 2
        return exc.code


@pytest.mark.parametrize(
    ("principal_type", "profile"),
    [
        ("human", "reception-staff-demo"),
        ("integration", "administrador"),
        ("agent", "administrador"),
        ("agent", "secretaria"),
        ("system", "secretaria"),
    ],
)
def test_cli_refuses_a_profile_that_does_not_match_the_type(
    monkeypatch, maker, session, principal_type, profile
):
    code = _run_cli(
        monkeypatch,
        maker,
        ["issue", "--name", "mismatch", "--type", principal_type, "--profile", profile],
    )
    assert code == 2
    assert session.scalar(
        select(func.count()).select_from(Principal).where(Principal.display_name == "mismatch")
    ) == 0


def test_cli_issues_a_human_credential_and_prints_the_token_once(
    monkeypatch, maker, session, capsys
):
    code = _run_cli(
        monkeypatch,
        maker,
        ["issue", "--name", "Lucía Ramos", "--type", "human", "--profile", "secretaria"],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("ofk_") == 1
    principal = session.scalar(select(Principal).where(Principal.display_name == "Lucía Ramos"))
    assert principal.type == "human"
    role_codes = session.scalars(
        select(Role.code)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .join(Membership, Membership.id == RoleAssignment.membership_id)
        .where(Membership.principal_id == principal.id)
    ).all()
    assert role_codes == ["staff-secretaria"]


def test_the_same_human_name_in_two_orgs_is_two_people(session):
    org_b = _other_org(session)
    in_a = _resolve_principal(
        session, organization_id=ORG, name="Lucía Ramos", principal_type="human"
    )
    in_b = _resolve_principal(
        session, organization_id=org_b, name="Lucía Ramos", principal_type="human"
    )
    again_a = _resolve_principal(
        session, organization_id=ORG, name="Lucía Ramos", principal_type="human"
    )
    session.commit()
    assert in_a.id != in_b.id
    assert again_a.id == in_a.id


def test_integration_profiles_keep_their_prefixed_role(session):
    principal = _resolve_principal(
        session, organization_id=ORG, name="inbound-idn", principal_type="integration"
    )
    _assign_profile(session, organization_id=ORG, principal_id=principal.id, profile="n8n-inbound")
    session.commit()
    role = session.scalar(
        select(Role)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .join(Membership, Membership.id == RoleAssignment.membership_id)
        .where(Membership.principal_id == principal.id)
    )
    assert (role.code, role.name) == ("integration-n8n-inbound", "Integration: n8n-inbound")


# --- seed -------------------------------------------------------------------


def _credential_count(session) -> int:
    value = session.scalar(select(func.count()).select_from(IntegrationCredential))
    session.rollback()
    return value


def test_seed_writes_staff_and_human_tokens_privately(client, session, tmp_path, capsys):
    from scripts.seed_demo import issue_staff_credential

    env_path = tmp_path / ".env.demo.local"
    issue_staff_credential(session, organization_id=ORG, env_path=env_path, include_humans=True)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("BACKEND_DEMO_TOKEN=ofk_")
    name, value = lines[1].split("=", 1)
    assert name == "BACKEND_DEMO_HUMANS"
    humans = json.loads(value)
    assert [(h["role"], h["display_name"]) for h in humans] == [
        ("secretaria", "Lucía Ramos"),
        ("administrador", "Carlos Vega"),
    ]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    for token in [lines[0].split("=", 1)[1]] + [h["token"] for h in humans]:
        assert token not in out
    for human in humans:
        me = client.get("/me", headers={"Authorization": f"Bearer {human['token']}"})
        assert me.status_code == 200, me.text
        assert me.json()["principal"]["display_name"] == human["display_name"]
        assert me.json()["principal"]["type"] == "human"

    issue_staff_credential(session, organization_id=ORG, env_path=env_path, include_humans=True)
    session.expire_all()
    for display_name in ("Lucía Ramos", "Carlos Vega", "reception-staff-demo"):
        assert session.scalar(
            select(func.count()).select_from(Principal).where(Principal.display_name == display_name)
        ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(Role)
        .where(Role.code.in_(("staff-secretaria", "staff-administrador")))
    ) == 2
    session.rollback()


def test_seed_failure_commits_no_credential_and_writes_no_file(session, tmp_path):
    from scripts.seed_demo import issue_staff_credential

    lucia = _resolve_principal(
        session, organization_id=ORG, name="Lucía Ramos", principal_type="human"
    )
    _assign_profile(session, organization_id=ORG, principal_id=lucia.id, profile="administrador")
    session.commit()
    before = _credential_count(session)
    env_path = tmp_path / ".env.demo.local"

    with pytest.raises(RuntimeError):
        issue_staff_credential(
            session, organization_id=ORG, env_path=env_path, include_humans=True
        )

    session.rollback()
    assert not env_path.exists()
    assert _credential_count(session) == before


def test_seed_main_issues_the_human_credentials(monkeypatch, tmp_path):
    import scripts.seed_demo as seed

    calls = {}
    monkeypatch.setattr(seed, "seed_demo", lambda *a, **k: {})
    monkeypatch.setattr(seed, "assert_local_database_url", lambda *a, **k: None)
    monkeypatch.setattr(
        seed, "issue_staff_credential", lambda session, **kwargs: calls.update(kwargs)
    )
    from app.config import get_settings

    seed.main(
        [
            "--database-url",
            get_settings().test_database_url,
            "--issue-staff-credential",
            "--env-file",
            str(tmp_path / "x"),
        ]
    )
    assert calls["include_humans"] is True
