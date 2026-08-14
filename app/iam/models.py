"""The PF2 authorization domain (PF0 §10–§11).

Five tables plus one global identity table:

``principals``        — one row per actor that can issue a command (§10).
``memberships``       — the *only* link from a global principal to a tenant.
``permissions``       — the platform catalog, seeded by migration (M5).
``roles``             — tenant-owned bundles of platform permissions (T3/M3).
``role_permissions``  — which permissions a role carries.
``role_assignments``  — a role granted to a membership, organization-wide
                        (``location_id IS NULL``) or at exactly one location.

Every cross-tenant relational state is made **structurally impossible** by the
composite-FK pattern PF1 established (§7.1): the child carries
``organization_id`` and references its parent by ``(organization_id, id)``, so
the two tenants are the same value by construction. No trigger and no
application check participates.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: The closed principal-type vocabulary (PR1/A10), enforced by a CHECK so a
#: fifth kind of actor cannot appear by accident the way today's free-form
#: ``audit_events.actor_type`` allows.
PRINCIPAL_TYPES: tuple[str, ...] = ("human", "agent", "integration", "system")
PRINCIPAL_TYPE_CHECK = "type IN ('human', 'agent', 'integration', 'system')"

#: The platform principal seeded by migration ``0003`` (PR6): the actor for
#: migrations, backfills and platform maintenance. Its id is pinned so
#: application code and tests can resolve it deterministically.
SYSTEM_PRINCIPAL_ID = 1
SYSTEM_PRINCIPAL_TYPE = "system"
SYSTEM_PRINCIPAL_DISPLAY_NAME = "system"

#: The role every organization grants the system principal (PR7). It is data,
#: never a code branch: services ask for permission codes (M9).
SYSTEM_ROLE_CODE = "system"
SYSTEM_ROLE_NAME = "System"


class Principal(Base):
    """A GLOBAL actor identity (PR3/T2): humans, agents, integrations, system.

    Deliberately **vendor-blind** (PR2): no ``provider``, ``vendor``, ``model``,
    ``framework``, ``endpoint`` or ``api_key`` column ever appears here, so
    swapping an agent's underlying model changes nothing in authorization or
    audit. Authority is never on this row — it lives on :class:`Membership` plus
    :class:`RoleAssignment`.
    """

    __tablename__ = "principals"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(250), nullable=False)
    #: The auth subject, once authentication exists (PF3). NULLs are distinct in
    #: PostgreSQL, so many principals may have no subject yet.
    external_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The platform kill switch (PR5): an inactive principal is refused at
    #: identity resolution, in every organization at once.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(PRINCIPAL_TYPE_CHECK, name="ck_principals_type"),
        UniqueConstraint("external_subject", name="uq_principals_external_subject"),
    )


class Membership(Base):
    """The sole tenant authority link (M10): principal × organization.

    An inactive membership grants **zero** effective permissions no matter how
    many role assignments reference it — the flag is re-read live on every
    evaluation (§12 E2/E3), so revocation needs no cache invalidation.
    """

    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_memberships_organization"),
        nullable=False,
    )
    principal_id: Mapped[int] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_memberships_principal"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "principal_id", name="uq_memberships_organization_principal"
        ),
        # The tenant-qualified referenced key ``role_assignments`` points at, so
        # a role can never be assigned through another organization's membership.
        UniqueConstraint("organization_id", "id", name="uq_memberships_organization_id"),
        Index("ix_memberships_principal", "principal_id"),
    )


class Permission(Base):
    """A PLATFORM catalog row (T3/M5) — no ``organization_id``, never tenant data.

    Seeded by migration ``0003`` from
    :data:`app.iam.permissions.PERMISSION_CATALOG` and never inserted through an
    application surface.
    """

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)

    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)


class Role(Base):
    """A tenant-owned bundle of platform permissions (T3/M3).

    ``organization_id`` is NOT NULL and ``role_assignments`` references
    ``roles(organization_id, id)``, so assigning organization B's role inside
    organization A is a foreign-key violation. There are no global role
    templates in PF2 (§18).
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_roles_organization"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Per organization, so two tenants may both run a role called "owner"
        # without sharing a row.
        UniqueConstraint("organization_id", "code", name="uq_roles_organization_code"),
        UniqueConstraint("organization_id", "id", name="uq_roles_organization_id"),
    )


class RolePermission(Base):
    """Role × Permission. No tenant column is needed or possible to misuse.

    ``role_id`` is globally unique and organization-owned and ``permission_id``
    is platform-global, so no cross-tenant state is expressible here (§6).
    """

    __tablename__ = "role_permissions"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="RESTRICT", name="fk_role_permissions_role"),
        nullable=False,
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey(
            "permissions.id", ondelete="RESTRICT", name="fk_role_permissions_permission"
        ),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),
        Index("ix_role_permissions_permission", "permission_id"),
    )


class RoleAssignment(Base):
    """A role granted through a membership, optionally narrowed to one location.

    Scope is **concrete, not polymorphic** (M1): the only scope dimension is a
    nullable ``location_id`` FK — ``NULL`` means organization-wide, a value
    means that location only. A ``scope_type``/``scope_id`` pair could not be
    constrained by a foreign key, which is exactly what §7 exists to eliminate.

    Three composite FKs keep membership, role and location inside the same
    organization. All three rely on PostgreSQL's default **MATCH SIMPLE**
    (§7.3): the location check is skipped when ``location_id IS NULL``, which is
    what makes the organization-wide encoding legal. ``MATCH FULL`` would reject
    it and must never be used here.
    """

    __tablename__ = "role_assignments"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id", ondelete="RESTRICT", name="fk_role_assignments_organization"
        ),
        nullable=False,
    )
    membership_id: Mapped[int] = mapped_column(nullable=False)
    role_id: Mapped[int] = mapped_column(nullable=False)
    #: NULL = organization-wide; a value = limited to that Location (M1/E4).
    location_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization_role",
        ),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization_location",
        ),
        # Two partial unique indexes, not one nullable UNIQUE (M4): PostgreSQL
        # treats NULLs as distinct, so a plain UNIQUE over the triple would let
        # the same organization-wide role be assigned twice.
        Index(
            "uq_role_assignment_scoped",
            "membership_id",
            "role_id",
            "location_id",
            unique=True,
            postgresql_where=text("location_id IS NOT NULL"),
        ),
        Index(
            "uq_role_assignment_org_wide",
            "membership_id",
            "role_id",
            unique=True,
            postgresql_where=text("location_id IS NULL"),
        ),
    )
