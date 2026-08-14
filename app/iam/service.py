"""Authorization evaluation and the minimal IAM provisioning surface (PF0 §12).

The whole authorization contract is one query. It joins
``memberships → role_assignments → role_permissions → permissions``, filters on
the live membership flag and the location scope, and answers yes or no:

.. code-block:: sql

    SELECT 1
      FROM memberships m
      JOIN role_assignments ra ON ra.membership_id = m.id
      JOIN role_permissions rp ON rp.role_id       = ra.role_id
      JOIN permissions p       ON p.id             = rp.permission_id
     WHERE m.organization_id = :organization_id
       AND m.principal_id    = :principal_id
       AND m.is_active
       AND p.code            = :code
       AND (ra.location_id IS NULL OR ra.location_id = :location_id)
     LIMIT 1;

Properties that follow from that single statement:

* **Deny by default** (A9/E1) — no matching row is a denial, and the denial is
  one deterministic error that never reveals which condition failed.
* **No role-name logic anywhere** (M9) — ``roles.code`` and ``roles.name`` do
  not appear in this module or in any decision. Renaming a role cannot change
  an outcome; only ``role_permissions`` rows can.
* **No branching on principal type** (P7) — a human, an agent, an integration
  and the system principal traverse the identical path with the identical
  inputs. There is no privileged bypass to bypass.
* **Evaluated live, per command** (E2/E3) — nothing is cached, memoized or
  precomputed, so deactivating a membership takes effect on the next command.
* **Never derived from model output** (E7) — the only inputs are the
  ``principals`` row and the organization's membership/role data.
"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import literal, or_, select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.iam.context import ExecutionContext
from app.iam.models import (
    SYSTEM_PRINCIPAL_ID,
    SYSTEM_ROLE_CODE,
    SYSTEM_ROLE_NAME,
    Membership,
    Permission,
    Principal,
    Role,
    RoleAssignment,
    RolePermission,
)
from app.iam.permissions import PERMISSION_CODES


class IamErrorCode(str, Enum):
    """PF0 §12 E9's new code, rendered through the existing stable envelope.

    ``app/errors.py`` is outside PF2's write surface for this task, so the code
    is declared here and always constructed with an explicit message and HTTP
    status. :class:`app.errors.AppError` only consults its lookup tables when
    those are omitted, and the registered handler renders ``code.value``, so the
    response body is byte-identical to every other error:
    ``{"error": {"code": "PERMISSION_DENIED", "message": ..., "details": {}}}``.
    Promoting the entry into ``HTTP_STATUS_BY_CODE`` / ``ErrorCode`` is a
    one-line follow-up for the block that owns the error contract.
    """

    PERMISSION_DENIED = "PERMISSION_DENIED"


PERMISSION_DENIED_HTTP_STATUS = 403
PERMISSION_DENIED_MESSAGE = "The principal is not authorized to perform this action."


# --- evaluation -------------------------------------------------------------


def has_permission(
    session: Session,
    principal_id: int,
    organization_id: int,
    permission_code: str,
    location_id: int | None = None,
) -> bool:
    """Does this principal hold ``permission_code`` in this organization, in scope?

    ``location_id=None`` asks about an organization-level operation and is
    satisfied **only** by an organization-wide grant (E5); a concrete
    ``location_id`` is satisfied by an organization-wide grant or by a grant
    scoped to that same location, and by nothing else (E4).
    """
    if location_id is None:
        # A location-less check requires an organization-wide grant. A grant
        # scoped to a branch is strictly narrower and never widens to the org.
        in_scope = RoleAssignment.location_id.is_(None)
    else:
        in_scope = or_(
            RoleAssignment.location_id.is_(None),
            RoleAssignment.location_id == location_id,
        )

    statement = (
        select(literal(1))
        .select_from(Membership)
        .join(RoleAssignment, RoleAssignment.membership_id == Membership.id)
        .join(RolePermission, RolePermission.role_id == RoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            Membership.organization_id == organization_id,
            Membership.principal_id == principal_id,
            # Re-read on every evaluation: an inactive membership resolves zero
            # effective permissions, immediately (M10/E3/F-5).
            Membership.is_active.is_(True),
            Permission.code == permission_code,
            in_scope,
        )
        .limit(1)
    )
    return session.scalar(statement) is not None


def require_permission(
    session: Session,
    ctx: ExecutionContext,
    code: str,
    *,
    location_id: int | None = None,
) -> None:
    """The single authorization entry point (§12 E6).

    Called as the first statement of an application command service, with the
    same context the mutation uses, so a transport that forgets to check can
    never reach a mutation. One rule, one place, all transports equally covered.
    """
    if has_permission(session, ctx.principal_id, ctx.organization_id, code, location_id):
        return
    raise AppError(
        IamErrorCode.PERMISSION_DENIED,
        PERMISSION_DENIED_MESSAGE,
        details={},
        http_status=PERMISSION_DENIED_HTTP_STATUS,
    )


def effective_permission_codes(
    session: Session,
    principal_id: int,
    organization_id: int,
    location_id: int | None = None,
) -> set[str]:
    """Every permission code this principal holds in scope — same joins as above.

    A convenience for provisioning tools and tests (it makes "a human and an
    agent with identical assignments resolve identical authority" a single
    assertion). It is **not** a caching surface: nothing stores the result, and
    the authoritative check stays :func:`require_permission` (E2/X4).
    """
    if location_id is None:
        in_scope = RoleAssignment.location_id.is_(None)
    else:
        in_scope = or_(
            RoleAssignment.location_id.is_(None),
            RoleAssignment.location_id == location_id,
        )

    statement = (
        select(Permission.code)
        .select_from(Membership)
        .join(RoleAssignment, RoleAssignment.membership_id == Membership.id)
        .join(RolePermission, RolePermission.role_id == RoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            Membership.organization_id == organization_id,
            Membership.principal_id == principal_id,
            Membership.is_active.is_(True),
            in_scope,
        )
        .distinct()
    )
    return set(session.scalars(statement))


# --- provisioning -----------------------------------------------------------
#
# Deliberately thin. PF2 owns identity and authorization data, not an admin API:
# there is no HTTP surface here (that is PF3) and no permission-catalog writer
# at all (M5 — the catalog is migration-owned).


def create_principal(
    session: Session,
    *,
    display_name: str,
    principal_type: str,
    external_subject: str | None = None,
    is_active: bool = True,
) -> Principal:
    """Register a global actor identity (PR3).

    ``principal_type`` is passed straight to PostgreSQL: the closed set is a
    CHECK constraint, so an unknown kind of actor is rejected by the database
    and not merely by a Python guard (A1/PR1).
    """
    principal = Principal(
        type=principal_type,
        display_name=display_name,
        external_subject=external_subject,
        is_active=is_active,
    )
    session.add(principal)
    session.commit()
    session.refresh(principal)
    return principal


def add_membership(
    session: Session, *, organization_id: int, principal_id: int, is_active: bool = True
) -> Membership:
    """Give a global principal reach into exactly one organization (T2/PR3)."""
    membership = Membership(
        organization_id=organization_id, principal_id=principal_id, is_active=is_active
    )
    session.add(membership)
    session.commit()
    session.refresh(membership)
    return membership


def set_membership_active(session: Session, membership_id: int, is_active: bool) -> Membership:
    """Activate or revoke a membership. Rows are never deleted (RESTRICT)."""
    membership = session.get(Membership, membership_id)
    if membership is None:
        raise LookupError(f"Membership {membership_id} does not exist.")
    membership.is_active = is_active
    session.commit()
    session.refresh(membership)
    return membership


def create_role(session: Session, *, organization_id: int, code: str, name: str) -> Role:
    """Create a tenant-owned role. Its code and name are labels, never logic (M9)."""
    role = Role(organization_id=organization_id, code=code, name=name)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def grant_permission(session: Session, *, role_id: int, permission_code: str) -> RolePermission:
    """Add one platform permission to a role.

    The permission must already exist: this surface resolves codes, it never
    inserts into ``permissions`` (M5).
    """
    permission_id = session.scalar(
        select(Permission.id).where(Permission.code == permission_code)
    )
    if permission_id is None:
        raise LookupError(f"Unknown permission code: {permission_code}.")
    role_permission = RolePermission(role_id=role_id, permission_id=permission_id)
    session.add(role_permission)
    session.commit()
    session.refresh(role_permission)
    return role_permission


def assign_role(
    session: Session,
    *,
    organization_id: int,
    membership_id: int,
    role_id: int,
    location_id: int | None = None,
) -> RoleAssignment:
    """Grant a role through a membership, organization-wide or at one location.

    ``location_id=None`` is the organization-wide grant. The composite FKs
    guarantee membership, role and location all belong to ``organization_id``;
    this function adds no equality check of its own precisely because the
    database is the authority (A1).
    """
    assignment = RoleAssignment(
        organization_id=organization_id,
        membership_id=membership_id,
        role_id=role_id,
        location_id=location_id,
    )
    session.add(assignment)
    session.commit()
    session.refresh(assignment)
    return assignment


def provision_system_access(session: Session, organization_id: int) -> Membership:
    """Give the seeded ``system`` principal full authority in one organization (PR7).

    Idempotent, and identical to what migration ``0003`` seeds for every
    organization that already existed: a ``system`` role holding the whole
    permission catalog, a membership, and an organization-wide assignment. A
    system-issued command is therefore permission-checked and audited on exactly
    the same path as a human or an agent — there is no bypass (P7).

    Runs inside the caller's transaction (it never commits), so organization
    creation can satisfy the PR7 invariant atomically.
    """
    role = session.scalar(
        select(Role).where(
            Role.organization_id == organization_id, Role.code == SYSTEM_ROLE_CODE
        )
    )
    if role is None:
        role = Role(
            organization_id=organization_id, code=SYSTEM_ROLE_CODE, name=SYSTEM_ROLE_NAME
        )
        session.add(role)
        session.flush()

    granted = set(
        session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role.id)
        )
    )
    for code in PERMISSION_CODES:
        if code in granted:
            continue
        permission_id = session.scalar(select(Permission.id).where(Permission.code == code))
        if permission_id is None:
            raise LookupError(f"Permission catalog row missing: {code}.")
        session.add(RolePermission(role_id=role.id, permission_id=permission_id))

    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == organization_id,
            Membership.principal_id == SYSTEM_PRINCIPAL_ID,
        )
    )
    if membership is None:
        membership = Membership(
            organization_id=organization_id, principal_id=SYSTEM_PRINCIPAL_ID, is_active=True
        )
        session.add(membership)
        session.flush()

    assignment = session.scalar(
        select(RoleAssignment).where(
            RoleAssignment.membership_id == membership.id,
            RoleAssignment.role_id == role.id,
            RoleAssignment.location_id.is_(None),
        )
    )
    if assignment is None:
        session.add(
            RoleAssignment(
                organization_id=organization_id,
                membership_id=membership.id,
                role_id=role.id,
                location_id=None,
            )
        )
    session.flush()
    return membership
