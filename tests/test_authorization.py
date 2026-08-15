"""PF2 — Principal identity and authorization proofs against real PostgreSQL.

Two kinds of proof live here.

**Database proofs** write the forbidden row with raw SQL, deliberately bypassing
every application service, because the invariant under test is a database
invariant (PF0 A1/§7): a role assigned through another organization's
membership, or scoped to another organization's location, must be impossible
even if every application check were absent, bypassed or buggy.

**Evaluation proofs** exercise the authorization service directly with
constructed contexts (§13 X7). PF2 adds no HTTP enforcement — wiring transports
is PF3 (§21) — so the guard is proven exactly where it is authoritative: at the
application-service boundary.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.iam import service as iam_service
from app.iam.context import ExecutionContext
from app.iam.models import (
    PRINCIPAL_TYPES,
    SYSTEM_PRINCIPAL_ID,
    SYSTEM_ROLE_CODE,
    Membership,
    Permission,
    Principal,
    Role,
    RoleAssignment,
    RolePermission,
)
from app.iam.permissions import (
    APPOINTMENTS_CANCEL,
    APPOINTMENTS_CREATE,
    APPOINTMENTS_READ,
    PERMISSION_CODES,
    SERVICES_MANAGE,
    SERVICES_READ,
)
from app.iam.service import (
    IamErrorCode,
    add_membership,
    assign_role,
    create_principal,
    create_role,
    effective_permission_codes,
    grant_permission,
    has_permission,
    provision_system_access,
    require_permission,
    set_membership_active,
)
from app.errors import AppError
from app.organization.schemas import LocationCreate
from app.organization.service import create_location, create_organization
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"
LIMA = "America/Lima"

PRINCIPAL_INSERT = "INSERT INTO principals (type, display_name) VALUES (:type, :name)"
MEMBERSHIP_INSERT = (
    "INSERT INTO memberships (organization_id, principal_id) VALUES (:org, :principal)"
)
ROLE_INSERT = "INSERT INTO roles (organization_id, code, name) VALUES (:org, :code, :name)"
ROLE_PERMISSION_INSERT = (
    "INSERT INTO role_permissions (role_id, permission_id) VALUES (:role, :permission)"
)
ASSIGNMENT_INSERT = (
    "INSERT INTO role_assignments (organization_id, membership_id, role_id, location_id)"
    " VALUES (:org, :membership, :role, :location)"
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


def context_for(principal, organization_id: int) -> ExecutionContext:
    """A complete, frozen context for a service-level authorization check (X2/X7)."""
    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal.id,
        principal_type=principal.type,
        request_id="req-test",
        correlation_id="corr-test",
    )


def seed_tenant(session, name: str, *, locations: int = 2) -> dict:
    """One organization with ``locations`` branches, built through the services."""
    organization_id = create_organization(session, name).id
    location_ids = [
        create_location(
            session, LocationCreate(name=f"{name} sede {n}", timezone=LIMA), organization_id
        ).id
        for n in range(1, locations + 1)
    ]
    return {"organization_id": organization_id, "location_ids": location_ids}


def seed_role(session, organization_id: int, code: str, *codes: str) -> Role:
    """A tenant role holding the given permission codes."""
    role = create_role(session, organization_id=organization_id, code=code, name=code.title())
    for permission_code in codes:
        grant_permission(session, role_id=role.id, permission_code=permission_code)
    return role


@pytest.fixture
def two_orgs(session):
    return seed_tenant(session, "Clinica A"), seed_tenant(session, "Clinica B")


# --- 1. Principal identity (§10) -------------------------------------------


@pytest.mark.parametrize("principal_type", PRINCIPAL_TYPES)
def test_every_closed_set_principal_type_is_accepted(session, principal_type):
    principal = create_principal(
        session, display_name=f"{principal_type} actor", principal_type=principal_type
    )
    assert principal.id is not None
    assert principal.type == principal_type
    assert principal.is_active is True
    # Vendor-blind (PR2): nothing about provider, model or framework is stored.
    columns = set(Principal.__table__.columns.keys())
    assert columns == {
        "id",
        "type",
        "display_name",
        "external_subject",
        "is_active",
        "created_at",
    }


def test_unknown_principal_type_is_rejected_by_the_database(session):
    error = refused_by_database(
        session, PRINCIPAL_INSERT, {"type": "robot", "name": "Unknown kind"}
    )
    assert "ck_principals_type" in error
    assert session.scalar(select(func.count()).select_from(Principal)) == 1  # only `system`


def test_principals_are_global_and_carry_no_tenant_column():
    # A Principal reaches a tenant only through a Membership (T2/PR3).
    assert "organization_id" not in Principal.__table__.columns


def test_external_subject_is_unique_but_many_principals_may_have_none(session):
    create_principal(
        session, display_name="Ana", principal_type="human", external_subject="auth|ana"
    )
    create_principal(session, display_name="Bot 1", principal_type="agent")
    create_principal(session, display_name="Bot 2", principal_type="agent")

    error = refused_by_database(
        session,
        "INSERT INTO principals (type, display_name, external_subject)"
        " VALUES ('human', 'Impostor', 'auth|ana')",
    )
    assert "uq_principals_external_subject" in error


# --- 2-3. Membership (§11) --------------------------------------------------


def test_one_principal_belongs_to_two_organizations(session, two_orgs):
    a, b = two_orgs
    principal = create_principal(session, display_name="Dra. Ana", principal_type="human")

    add_membership(session, organization_id=a["organization_id"], principal_id=principal.id)
    add_membership(session, organization_id=b["organization_id"], principal_id=principal.id)

    organizations = set(
        session.scalars(
            select(Membership.organization_id).where(Membership.principal_id == principal.id)
        )
    )
    assert organizations == {a["organization_id"], b["organization_id"]}


def test_duplicate_membership_is_rejected_by_the_database(session, two_orgs):
    a, _b = two_orgs
    principal = create_principal(session, display_name="Ana", principal_type="human")
    add_membership(session, organization_id=a["organization_id"], principal_id=principal.id)

    error = refused_by_database(
        session,
        MEMBERSHIP_INSERT,
        {"org": a["organization_id"], "principal": principal.id},
    )
    assert "uq_memberships_organization_principal" in error
    assert (
        session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.principal_id == principal.id)
        )
        == 1
    )


# --- 4. Inactive membership (§12 E3, F-5) -----------------------------------


def test_inactive_membership_resolves_zero_effective_permissions(session, two_orgs):
    a, _b = two_orgs
    org = a["organization_id"]
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership = add_membership(session, organization_id=org, principal_id=principal.id)
    role = seed_role(session, org, "reception", APPOINTMENTS_READ, APPOINTMENTS_CREATE)
    assign_role(
        session, organization_id=org, membership_id=membership.id, role_id=role.id
    )
    assert has_permission(session, principal.id, org, APPOINTMENTS_READ) is True

    set_membership_active(session, membership.id, False)

    # No cache to clear: the flag is re-read by the very next evaluation (E2/E3).
    assert has_permission(session, principal.id, org, APPOINTMENTS_READ) is False
    assert has_permission(session, principal.id, org, APPOINTMENTS_CREATE) is False
    assert effective_permission_codes(session, principal.id, org) == set()
    # The role assignments still exist — authority is lost through the membership.
    assert (
        session.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(RoleAssignment.membership_id == membership.id)
        )
        == 1
    )

    set_membership_active(session, membership.id, True)
    assert has_permission(session, principal.id, org, APPOINTMENTS_READ) is True


# --- 5. Roles are tenant-owned (M3) ----------------------------------------


def test_the_same_role_code_may_exist_in_two_organizations(session, two_orgs):
    a, b = two_orgs
    role_a = create_role(
        session, organization_id=a["organization_id"], code="owner", name="Owner"
    )
    role_b = create_role(
        session, organization_id=b["organization_id"], code="owner", name="Owner"
    )

    assert role_a.id != role_b.id
    assert role_a.organization_id != role_b.organization_id


def test_duplicate_role_code_inside_one_organization_is_rejected(session, two_orgs):
    a, _b = two_orgs
    create_role(session, organization_id=a["organization_id"], code="owner", name="Owner")

    error = refused_by_database(
        session,
        ROLE_INSERT,
        {"org": a["organization_id"], "code": "owner", "name": "Owner again"},
    )
    assert "uq_roles_organization_code" in error


# --- 6. RolePermission resolves the permission (§12) ------------------------


def test_grant_and_deny_matrix_per_permission_code(session, two_orgs):
    a, b = two_orgs
    org = a["organization_id"]
    holder = create_principal(session, display_name="Holder", principal_type="human")
    non_holder = create_principal(session, display_name="Non holder", principal_type="human")
    outsider = create_principal(session, display_name="Outsider", principal_type="human")

    holder_membership = add_membership(
        session, organization_id=org, principal_id=holder.id
    )
    non_holder_membership = add_membership(
        session, organization_id=org, principal_id=non_holder.id
    )
    reader = seed_role(session, org, "reader", SERVICES_READ, APPOINTMENTS_READ)
    empty = create_role(session, organization_id=org, code="empty", name="Empty")
    assign_role(
        session, organization_id=org, membership_id=holder_membership.id, role_id=reader.id
    )
    assign_role(
        session,
        organization_id=org,
        membership_id=non_holder_membership.id,
        role_id=empty.id,
    )

    # Holder → allowed for exactly the codes its role carries, denied elsewhere.
    assert has_permission(session, holder.id, org, SERVICES_READ) is True
    assert has_permission(session, holder.id, org, APPOINTMENTS_READ) is True
    assert has_permission(session, holder.id, org, SERVICES_MANAGE) is False
    assert effective_permission_codes(session, holder.id, org) == {
        SERVICES_READ,
        APPOINTMENTS_READ,
    }
    # No implication (M8): `services.manage` is not granted by `services.read`.
    require_permission(session, context_for(holder, org), SERVICES_READ)
    with pytest.raises(AppError):
        require_permission(session, context_for(holder, org), SERVICES_MANAGE)

    # Role assigned but holding no permission → denied.
    assert has_permission(session, non_holder.id, org, SERVICES_READ) is False
    # No membership at all → denied (deny by default, E1/A9).
    assert has_permission(session, outsider.id, org, SERVICES_READ) is False
    # A member of A asking inside B → denied; membership is per organization.
    assert has_permission(session, holder.id, b["organization_id"], SERVICES_READ) is False
    # An unknown permission code is simply a denial, never an error.
    assert has_permission(session, holder.id, org, "does.not.exist") is False


def test_permission_denied_uses_the_stable_envelope_contract(session, two_orgs):
    a, _b = two_orgs
    org = a["organization_id"]
    principal = create_principal(session, display_name="Ana", principal_type="human")
    add_membership(session, organization_id=org, principal_id=principal.id)

    with pytest.raises(AppError) as exc:
        require_permission(session, context_for(principal, org), APPOINTMENTS_CANCEL)

    assert exc.value.code is IamErrorCode.PERMISSION_DENIED
    assert exc.value.code.value == "PERMISSION_DENIED"
    assert exc.value.http_status == 403
    # Denial reveals nothing: not which condition failed, not whether the
    # resource exists (E1/E8).
    assert exc.value.details == {}


# --- 7-9. Location scope (§12 E4/E5, M1) ------------------------------------


@pytest.fixture
def scoped_setup(session, two_orgs):
    """One principal per scope shape, sharing one role, inside organization A."""
    a, b = two_orgs
    org = a["organization_id"]
    location_a, location_b = a["location_ids"]
    role = seed_role(session, org, "reception", APPOINTMENTS_READ, APPOINTMENTS_CREATE)

    org_wide = create_principal(session, display_name="Org wide", principal_type="human")
    scoped = create_principal(session, display_name="Scoped", principal_type="human")
    org_wide_membership = add_membership(
        session, organization_id=org, principal_id=org_wide.id
    )
    scoped_membership = add_membership(session, organization_id=org, principal_id=scoped.id)
    assign_role(
        session, organization_id=org, membership_id=org_wide_membership.id, role_id=role.id
    )
    assign_role(
        session,
        organization_id=org,
        membership_id=scoped_membership.id,
        role_id=role.id,
        location_id=location_a,
    )
    return {
        "org": org,
        "other_org": b,
        "location_a": location_a,
        "location_b": location_b,
        "role": role,
        "org_wide": org_wide,
        "scoped": scoped,
        "scoped_membership": scoped_membership,
        "org_wide_membership": org_wide_membership,
    }


def test_org_wide_assignment_authorizes_at_every_location(session, scoped_setup):
    s = scoped_setup
    principal, org = s["org_wide"], s["org"]

    # location_id IS NULL satisfies both concrete locations and the
    # location-less (organization-level) operation (E4).
    assert has_permission(session, principal.id, org, APPOINTMENTS_READ) is True
    assert (
        has_permission(session, principal.id, org, APPOINTMENTS_READ, s["location_a"]) is True
    )
    assert (
        has_permission(session, principal.id, org, APPOINTMENTS_READ, s["location_b"]) is True
    )
    require_permission(
        session, context_for(principal, org), APPOINTMENTS_READ, location_id=s["location_b"]
    )


def test_location_scoped_assignment_authorizes_only_its_own_location(session, scoped_setup):
    s = scoped_setup
    principal, org = s["scoped"], s["org"]

    assert (
        has_permission(session, principal.id, org, APPOINTMENTS_READ, s["location_a"]) is True
    )
    require_permission(
        session, context_for(principal, org), APPOINTMENTS_READ, location_id=s["location_a"]
    )


def test_location_scoped_assignment_denies_another_location(session, scoped_setup):
    s = scoped_setup
    principal, org = s["scoped"], s["org"]

    assert (
        has_permission(session, principal.id, org, APPOINTMENTS_READ, s["location_b"]) is False
    )
    with pytest.raises(AppError) as exc:
        require_permission(
            session,
            context_for(principal, org),
            APPOINTMENTS_READ,
            location_id=s["location_b"],
        )
    assert exc.value.code is IamErrorCode.PERMISSION_DENIED


def test_location_scoped_assignment_denies_a_location_less_operation(session, scoped_setup):
    # E5: a location-less check matches only an organization-wide grant, so a
    # scoped grant never silently widens to the whole organization.
    s = scoped_setup
    assert has_permission(session, s["scoped"].id, s["org"], APPOINTMENTS_READ) is False
    assert effective_permission_codes(session, s["scoped"].id, s["org"]) == set()
    assert effective_permission_codes(
        session, s["scoped"].id, s["org"], s["location_a"]
    ) == {APPOINTMENTS_READ, APPOINTMENTS_CREATE}


def test_the_same_role_may_be_scoped_to_several_locations(session, scoped_setup):
    s = scoped_setup
    assign_role(
        session,
        organization_id=s["org"],
        membership_id=s["scoped_membership"].id,
        role_id=s["role"].id,
        location_id=s["location_b"],
    )
    for location_id in (s["location_a"], s["location_b"]):
        assert (
            has_permission(
                session, s["scoped"].id, s["org"], APPOINTMENTS_READ, location_id
            )
            is True
        )
    # Still no organization-wide authority.
    assert has_permission(session, s["scoped"].id, s["org"], APPOINTMENTS_READ) is False


# --- 10-11. Cross-tenant assignments are impossible (F-3, F-6) --------------


def test_a_role_from_another_organization_cannot_be_assigned(session, two_orgs):
    a, b = two_orgs
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership = add_membership(
        session, organization_id=a["organization_id"], principal_id=principal.id
    )
    role_b = seed_role(session, b["organization_id"], "owner", SERVICES_MANAGE)

    error = refused_by_database(
        session,
        ASSIGNMENT_INSERT,
        {
            "org": a["organization_id"],
            "membership": membership.id,
            "role": role_b.id,  # organization B's role
            "location": None,
        },
    )
    assert "fk_role_assignments_organization_role" in error
    assert session.scalar(select(func.count()).select_from(RoleAssignment)) == 1  # system only


def test_a_membership_from_another_organization_cannot_be_assigned(session, two_orgs):
    a, b = two_orgs
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership_b = add_membership(
        session, organization_id=b["organization_id"], principal_id=principal.id
    )
    role_a = seed_role(session, a["organization_id"], "owner", SERVICES_MANAGE)

    error = refused_by_database(
        session,
        ASSIGNMENT_INSERT,
        {
            "org": a["organization_id"],
            "membership": membership_b.id,  # organization B's membership
            "role": role_a.id,
            "location": None,
        },
    )
    assert "fk_role_assignments_organization_membership" in error


def test_an_assignment_cannot_be_scoped_to_another_tenants_location(session, two_orgs):
    a, b = two_orgs
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership = add_membership(
        session, organization_id=a["organization_id"], principal_id=principal.id
    )
    role_a = seed_role(session, a["organization_id"], "reception", APPOINTMENTS_READ)

    error = refused_by_database(
        session,
        ASSIGNMENT_INSERT,
        {
            "org": a["organization_id"],
            "membership": membership.id,
            "role": role_a.id,
            "location": b["location_ids"][0],  # organization B's branch
        },
    )
    assert "fk_role_assignments_organization_location" in error


def test_an_org_wide_assignment_is_accepted_by_the_nullable_composite_fk(session, two_orgs):
    # MATCH SIMPLE, never MATCH FULL (§7.3): a NULL location must skip the
    # composite location check, which is what encodes "organization-wide".
    a, _b = two_orgs
    org = a["organization_id"]
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership = add_membership(session, organization_id=org, principal_id=principal.id)
    role = seed_role(session, org, "reception", APPOINTMENTS_READ)

    session.execute(
        text(ASSIGNMENT_INSERT),
        {"org": org, "membership": membership.id, "role": role.id, "location": None},
    )
    session.commit()

    assert has_permission(session, principal.id, org, APPOINTMENTS_READ) is True


def test_a_role_assignment_needs_a_tenant(session, two_orgs):
    a, _b = two_orgs
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership = add_membership(
        session, organization_id=a["organization_id"], principal_id=principal.id
    )
    role = seed_role(session, a["organization_id"], "reception", APPOINTMENTS_READ)

    error = refused_by_database(
        session,
        ASSIGNMENT_INSERT,
        {"org": None, "membership": membership.id, "role": role.id, "location": None},
    )
    assert "organization_id" in error


# --- Duplicate relation rows (M4, role_permissions) -------------------------


def test_duplicate_org_wide_assignment_is_rejected(session, scoped_setup):
    s = scoped_setup
    error = refused_by_database(
        session,
        ASSIGNMENT_INSERT,
        {
            "org": s["org"],
            "membership": s["org_wide_membership"].id,
            "role": s["role"].id,
            "location": None,
        },
    )
    # The partial index, not a plain nullable UNIQUE, is what catches this: in
    # PostgreSQL NULLs are distinct, so the triple alone would allow it (M4).
    assert "uq_role_assignment_org_wide" in error


def test_duplicate_scoped_assignment_is_rejected(session, scoped_setup):
    s = scoped_setup
    error = refused_by_database(
        session,
        ASSIGNMENT_INSERT,
        {
            "org": s["org"],
            "membership": s["scoped_membership"].id,
            "role": s["role"].id,
            "location": s["location_a"],
        },
    )
    assert "uq_role_assignment_scoped" in error


def test_duplicate_role_permission_is_rejected(session, two_orgs):
    a, _b = two_orgs
    role = seed_role(session, a["organization_id"], "reader", SERVICES_READ)
    permission_id = session.scalar(select(Permission.id).where(Permission.code == SERVICES_READ))

    error = refused_by_database(
        session, ROLE_PERMISSION_INSERT, {"role": role.id, "permission": permission_id}
    )
    assert "uq_role_permissions_role_permission" in error


# --- 12. Human and agent are evaluated identically (P7, F-9) ----------------


def test_human_and_agent_with_identical_assignments_resolve_identical_authority(
    session, two_orgs
):
    a, _b = two_orgs
    org = a["organization_id"]
    location_a, location_b = a["location_ids"]
    role = seed_role(session, org, "reception", APPOINTMENTS_READ, APPOINTMENTS_CREATE)

    human = create_principal(session, display_name="Ana", principal_type="human")
    agent = create_principal(session, display_name="Scheduler bot", principal_type="agent")
    for principal in (human, agent):
        membership = add_membership(session, organization_id=org, principal_id=principal.id)
        assign_role(
            session,
            organization_id=org,
            membership_id=membership.id,
            role_id=role.id,
            location_id=location_a,
        )

    for code in (APPOINTMENTS_READ, APPOINTMENTS_CREATE, APPOINTMENTS_CANCEL):
        for location_id in (None, location_a, location_b):
            assert has_permission(session, human.id, org, code, location_id) == has_permission(
                session, agent.id, org, code, location_id
            )
    assert effective_permission_codes(
        session, human.id, org, location_a
    ) == effective_permission_codes(session, agent.id, org, location_a)
    assert effective_permission_codes(session, agent.id, org, location_a) == {
        APPOINTMENTS_READ,
        APPOINTMENTS_CREATE,
    }
    # Deactivating only the agent's membership proves the two are independent
    # rows evaluated by the same mechanism, not by a type branch.
    agent_membership = session.scalar(
        select(Membership).where(
            Membership.principal_id == agent.id, Membership.organization_id == org
        )
    )
    set_membership_active(session, agent_membership.id, False)
    assert has_permission(session, agent.id, org, APPOINTMENTS_READ, location_a) is False
    assert has_permission(session, human.id, org, APPOINTMENTS_READ, location_a) is True


def test_the_evaluation_never_branches_on_principal_type():
    source = inspect.getsource(iam_service.has_permission) + inspect.getsource(
        iam_service.effective_permission_codes
    )
    assert "Principal." not in source
    assert "principal_type" not in source
    for principal_type in PRINCIPAL_TYPES:
        assert f'"{principal_type}"' not in source
        assert f"'{principal_type}'" not in source


# --- 13. Authorization never depends on role names (M9) ---------------------


def test_renaming_a_role_does_not_change_authorization(session, two_orgs):
    a, _b = two_orgs
    org = a["organization_id"]
    principal = create_principal(session, display_name="Ana", principal_type="human")
    membership = add_membership(session, organization_id=org, principal_id=principal.id)
    role = seed_role(session, org, "owner", SERVICES_READ)
    assign_role(session, organization_id=org, membership_id=membership.id, role_id=role.id)

    before = effective_permission_codes(session, principal.id, org)
    assert before == {SERVICES_READ}

    role.code = "nobody-in-particular"
    role.name = "Renamed"
    session.commit()

    # Behaviour depends only on role_permissions rows.
    assert effective_permission_codes(session, principal.id, org) == before
    assert has_permission(session, principal.id, org, SERVICES_READ) is True

    grant_permission(session, role_id=role.id, permission_code=SERVICES_MANAGE)
    assert has_permission(session, principal.id, org, SERVICES_MANAGE) is True


FORBIDDEN_ROLE_LITERALS = (
    "owner",
    "admin",
    "administrator",
    "superuser",
    "manager",
    "receptionist",
    "staff",
)


def app_sources() -> list[tuple[Path, str]]:
    return [(path, path.read_text()) for path in sorted(APP_ROOT.rglob("*.py"))]


def code_string_literals(source: str) -> list[str]:
    """Every string literal that is *code*, with comments and docstrings excluded."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_role_name_logic_anywhere_in_application_code():
    # M9: `if role == "owner"` (or any equivalent on roles.code / roles.name) is
    # forbidden in application, domain and transport code. Services ask for a
    # permission code; roles are data. Parsed, not grepped, so prose in comments
    # and docstrings cannot trip it and cannot hide a real branch either.
    offenders = []
    for path, source in app_sources():
        for literal in code_string_literals(source):
            if literal.lower() in FORBIDDEN_ROLE_LITERALS:
                offenders.append((str(path.relative_to(REPO_ROOT)), literal))
    assert offenders == []


def test_the_authorization_query_reads_no_role_name_column():
    for function in (
        iam_service.has_permission,
        iam_service.require_permission,
        iam_service.effective_permission_codes,
    ):
        source = inspect.getsource(function)
        assert "Role." not in source
        assert "role_code" not in source
        assert "role_name" not in source


# --- Permission catalog is code-owned (M5-M8) -------------------------------


def test_the_seeded_catalog_is_exactly_the_m7_closed_set(session):
    codes = set(session.scalars(select(Permission.code)))
    assert codes == set(PERMISSION_CODES)
    # 17 base codes + 6 clinical (PF5) codes.
    assert len(PERMISSION_CODES) == len(set(PERMISSION_CODES)) == 23


def test_every_permission_code_follows_the_naming_convention():
    # M6: <domain>.<action>, lowercase, dot-separated, no wildcards, no hierarchy.
    verbs = {"read", "create", "update", "cancel", "reschedule", "manage"}
    for code in PERMISSION_CODES:
        assert re.fullmatch(r"[a-z]+\.[a-z]+", code), code
        domain, action = code.split(".")
        assert action in verbs, code
        assert "*" not in code


def test_no_application_surface_writes_the_permission_catalog():
    # M5: permissions are seeded by migration and never inserted at runtime.
    offenders = []
    for path, source in app_sources():
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("class Permission("):
                continue
            if re.search(r"\bPermission\s*\(", stripped) or re.search(
                r"insert\s*\(\s*Permission", stripped
            ):
                offenders.append((path.relative_to(REPO_ROOT), stripped))
            if "permissions" in stripped and re.search(r"\bINSERT\s+INTO\b", stripped, re.I):
                offenders.append((path.relative_to(REPO_ROOT), stripped))
    assert offenders == []


# --- PR6/PR7: the seeded system principal -----------------------------------


def test_the_migration_seeds_exactly_one_system_principal(session):
    principals = session.scalars(select(Principal)).all()
    assert len(principals) == 1
    system = principals[0]
    assert system.id == SYSTEM_PRINCIPAL_ID
    assert system.type == "system"
    assert system.is_active is True


def test_the_system_principal_is_permission_checked_like_any_other(session):
    org = BOOTSTRAP_ORGANIZATION_ID
    # It holds a real membership and a real organization-wide role assignment —
    # there is no bypass path, only a fully granted role (PR7/P7).
    assert effective_permission_codes(session, SYSTEM_PRINCIPAL_ID, org) == set(
        PERMISSION_CODES
    )
    assert has_permission(session, SYSTEM_PRINCIPAL_ID, org, APPOINTMENTS_CREATE) is True
    # And it is denied in an organization where it holds no membership.
    other = create_organization(session, "Sin sistema").id
    assert has_permission(session, SYSTEM_PRINCIPAL_ID, other, APPOINTMENTS_CREATE) is False

    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == org, Membership.principal_id == SYSTEM_PRINCIPAL_ID
        )
    )
    set_membership_active(session, membership.id, False)
    assert has_permission(session, SYSTEM_PRINCIPAL_ID, org, APPOINTMENTS_CREATE) is False


def test_provision_system_access_grants_a_new_organization_and_is_idempotent(session):
    organization_id = create_organization(session, "Clinica Nueva").id

    provision_system_access(session, organization_id)
    session.commit()
    assert effective_permission_codes(session, SYSTEM_PRINCIPAL_ID, organization_id) == set(
        PERMISSION_CODES
    )

    provision_system_access(session, organization_id)
    session.commit()
    assert (
        session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.organization_id == organization_id)
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(RoleAssignment.organization_id == organization_id)
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(RolePermission)
            .join(Role, Role.id == RolePermission.role_id)
            .where(Role.organization_id == organization_id)
        )
        == len(PERMISSION_CODES)
    )
    role = session.scalar(
        select(Role).where(
            Role.organization_id == organization_id, Role.code == SYSTEM_ROLE_CODE
        )
    )
    assert role is not None


# --- Schema facts -----------------------------------------------------------


def test_pf2_constraints_exist_in_postgresql(session):
    names = set(
        session.execute(
            text("SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace")
        ).scalars()
    )
    assert {
        "ck_principals_type",
        "uq_principals_external_subject",
        "uq_memberships_organization_principal",
        "uq_memberships_organization_id",
        "uq_permissions_code",
        "uq_roles_organization_code",
        "uq_roles_organization_id",
        "uq_role_permissions_role_permission",
        "fk_role_assignments_organization_membership",
        "fk_role_assignments_organization_role",
        "fk_role_assignments_organization_location",
    } <= names

    indexes = set(
        session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        ).scalars()
    )
    assert {"uq_role_assignment_scoped", "uq_role_assignment_org_wide"} <= indexes


def test_every_pf2_foreign_key_restricts_deletion(session):
    actions = session.execute(
        text(
            "SELECT conname, confdeltype FROM pg_constraint WHERE contype = 'f'"
            " AND conrelid IN ('principals'::regclass, 'memberships'::regclass,"
            " 'roles'::regclass, 'role_permissions'::regclass,"
            " 'role_assignments'::regclass)"
        )
    ).all()
    assert actions, "PF2 tables must carry foreign keys"
    # 'r' = RESTRICT. No CASCADE and no SET NULL anywhere, consistent with 0001/0002.
    assert {action for _name, action in actions} == {"r"}


def test_the_execution_context_is_frozen_and_carries_no_authority():
    ctx = ExecutionContext(
        organization_id=1,
        principal_id=SYSTEM_PRINCIPAL_ID,
        principal_type="system",
        request_id="req-1",
        correlation_id="corr-1",
    )
    with pytest.raises(Exception):
        ctx.organization_id = 2  # frozen (X2)
    assert set(ExecutionContext.__dataclass_fields__) == {
        "organization_id",
        "principal_id",
        "principal_type",
        "request_id",
        "correlation_id",
    }
