"""Principal identity and permission-based authorization.

Implements PF2 of the platform foundation spec
(``docs/superpowers/specs/2026-08-14-platform-foundation-design.md``): the
global ``principals`` identity table (§10), ``memberships`` as the only link
from a principal to a tenant, the code-owned ``permissions`` catalog, tenant
owned ``roles`` with ``role_permissions``, and ``role_assignments`` carrying the
one concrete scope dimension — a nullable ``location_id`` (§11).

Strictly additive (A8): six new tables, no existing table altered, no row
touched, ``excl_appointments_confirmed_no_overlap`` untouched. The composite
foreign keys reuse PF1's pattern (§7.1), so a role assigned through another
organization's membership, or scoped to another organization's location, is a
foreign-key violation rather than an application concern.

Seeds, and only these (M5/PR6/PR7):

* the seventeen §11 M7 permission codes;
* the ``system`` principal;
* for every organization that already exists, a ``system`` role holding the
  whole catalog, the system principal's membership, and its organization-wide
  role assignment.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SYSTEM_PRINCIPAL_ID = 1
SYSTEM_PRINCIPAL_TYPE = "system"
SYSTEM_PRINCIPAL_DISPLAY_NAME = "system"
SYSTEM_ROLE_CODE = "system"
SYSTEM_ROLE_NAME = "System"

# The closed principal vocabulary (PR1/A10) — a CHECK, not a Python guard.
PRINCIPAL_TYPE_CHECK = "type IN ('human', 'agent', 'integration', 'system')"

# The closed permission catalog of §11 M7: exactly the surface today's eleven
# endpoints need. Convention (M6): ``<domain>.<action>``, no wildcards, no
# hierarchy, no implication between codes (M8). Mirrors
# ``app/iam/permissions.py``; duplicated here on purpose so the migration stays
# independent of application code, exactly as ``0002`` duplicates the bootstrap
# organization constants.
PERMISSION_CATALOG = (
    ("appointments.read", "Read appointments"),
    ("appointments.create", "Book appointments"),
    ("appointments.reschedule", "Reschedule appointments"),
    ("appointments.cancel", "Cancel appointments"),
    ("services.read", "Read services"),
    ("services.manage", "Administer services"),
    ("leads.read", "Read leads"),
    ("leads.create", "Register leads"),
    ("locations.read", "Read locations"),
    ("locations.manage", "Administer locations"),
    ("practitioners.read", "Read practitioners"),
    ("practitioners.manage", "Administer practitioners"),
    ("capabilities.read", "Read practitioner capabilities"),
    ("capabilities.manage", "Administer practitioner capabilities"),
    ("availability.read", "Read availability"),
    ("availability.manage", "Administer availability"),
    ("audit.read", "Read the audit trail"),
)


def upgrade() -> None:
    # --- global identity ----------------------------------------------------
    op.create_table(
        "principals",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=250), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(PRINCIPAL_TYPE_CHECK, name="ck_principals_type"),
        # NULLs are distinct in PostgreSQL, so many principals may still have no
        # auth subject while authentication does not exist (PF3).
        sa.UniqueConstraint("external_subject", name="uq_principals_external_subject"),
    )

    # --- tenant reach -------------------------------------------------------
    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_memberships_organization",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            ondelete="RESTRICT",
            name="fk_memberships_principal",
        ),
        sa.UniqueConstraint(
            "organization_id", "principal_id", name="uq_memberships_organization_principal"
        ),
        # The tenant-qualified referenced key ``role_assignments`` points at.
        sa.UniqueConstraint("organization_id", "id", name="uq_memberships_organization_id"),
    )
    op.create_index("ix_memberships_principal", "memberships", ["principal_id"])

    # --- platform catalog ---------------------------------------------------
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    # --- tenant-owned roles -------------------------------------------------
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_roles_organization",
        ),
        # Per organization: two tenants may both define a role code, and no
        # role row is ever shared between them (M3).
        sa.UniqueConstraint("organization_id", "code", name="uq_roles_organization_code"),
        sa.UniqueConstraint("organization_id", "id", name="uq_roles_organization_id"),
    )

    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], ondelete="RESTRICT", name="fk_role_permissions_role"
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            ondelete="RESTRICT",
            name="fk_role_permissions_permission",
        ),
        sa.UniqueConstraint(
            "role_id", "permission_id", name="uq_role_permissions_role_permission"
        ),
    )
    op.create_index("ix_role_permissions_permission", "role_permissions", ["permission_id"])

    # --- assignments (the only scope dimension: location_id) ----------------
    op.create_table(
        "role_assignments",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("membership_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        # NULL = organization-wide; a value = that location only (M1).
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization",
        ),
        # The three composite FKs of §11. MATCH SIMPLE (PostgreSQL's default) is
        # required and MATCH FULL is forbidden (§7.3): the location check must be
        # skipped when ``location_id IS NULL``, which is what encodes org-wide.
        sa.ForeignKeyConstraint(
            ["organization_id", "membership_id"],
            ["memberships.organization_id", "memberships.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization_membership",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_role_assignments_organization_location",
        ),
    )
    # Two partial unique indexes rather than one nullable UNIQUE (M4): NULLs are
    # distinct in PostgreSQL, so a plain UNIQUE over the triple would allow the
    # same organization-wide grant twice. No dependency on NULLS NOT DISTINCT.
    op.create_index(
        "uq_role_assignment_scoped",
        "role_assignments",
        ["membership_id", "role_id", "location_id"],
        unique=True,
        postgresql_where=sa.text("location_id IS NOT NULL"),
    )
    op.create_index(
        "uq_role_assignment_org_wide",
        "role_assignments",
        ["membership_id", "role_id"],
        unique=True,
        postgresql_where=sa.text("location_id IS NULL"),
    )

    # --- seed: the platform vocabulary and the system principal -------------
    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("code", sa.String),
            sa.column("name", sa.String),
        ),
        [{"code": code, "name": name} for code, name in PERMISSION_CATALOG],
    )

    # PR6: exactly one principal is ever created by a migration. Its id is
    # pinned so application code resolves it deterministically, then the
    # identity sequence is advanced past it.
    op.execute(
        sa.text(
            "INSERT INTO principals (id, type, display_name) VALUES (:id, :type, :name)"
        ).bindparams(
            id=SYSTEM_PRINCIPAL_ID,
            type=SYSTEM_PRINCIPAL_TYPE,
            name=SYSTEM_PRINCIPAL_DISPLAY_NAME,
        )
    )
    op.execute(
        f"ALTER TABLE principals ALTER COLUMN id RESTART WITH {SYSTEM_PRINCIPAL_ID + 1}"
    )

    # PR7: the system principal holds a membership and an organization-wide
    # ``system`` role in every organization, so platform automation is
    # permission-checked on the same path as a human or an agent — no bypass.
    op.execute(
        sa.text(
            "INSERT INTO roles (organization_id, code, name) "
            "SELECT o.id, :code, :name FROM organizations o"
        ).bindparams(code=SYSTEM_ROLE_CODE, name=SYSTEM_ROLE_NAME)
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.code = :code"
        ).bindparams(code=SYSTEM_ROLE_CODE)
    )
    op.execute(
        sa.text(
            "INSERT INTO memberships (organization_id, principal_id) "
            "SELECT o.id, :principal FROM organizations o"
        ).bindparams(principal=SYSTEM_PRINCIPAL_ID)
    )
    op.execute(
        sa.text(
            "INSERT INTO role_assignments (organization_id, membership_id, role_id) "
            "SELECT m.organization_id, m.id, r.id FROM memberships m "
            "JOIN roles r ON r.organization_id = m.organization_id AND r.code = :code "
            "WHERE m.principal_id = :principal"
        ).bindparams(code=SYSTEM_ROLE_CODE, principal=SYSTEM_PRINCIPAL_ID)
    )


def downgrade() -> None:
    op.drop_index("uq_role_assignment_org_wide", table_name="role_assignments")
    op.drop_index("uq_role_assignment_scoped", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_index("ix_role_permissions_permission", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_table("roles")
    op.drop_table("permissions")
    op.drop_index("ix_memberships_principal", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("principals")
