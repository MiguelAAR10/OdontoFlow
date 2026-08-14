"""Organization as tenant root, and tenant integrity enforced by PostgreSQL.

Implements PF1 of the platform foundation spec
(``docs/superpowers/specs/2026-08-14-platform-foundation-design.md``): the
``organizations`` tenant root, ``practitioner_memberships`` (a global
practitioner reaching a tenant), direct ``organization_id`` ownership on the
eight tenant-owned tables, and the composite foreign keys that make a
cross-tenant relational state *structurally impossible* rather than merely
rejected by application code (§7).

Staged exactly as §19.1 prescribes and strictly additive (A8): create and seed,
add nullable columns, backfill every Vertical 1 row into the bootstrap
organization, then tighten. No table is dropped or recreated, no row is
discarded, and ``excl_appointments_confirmed_no_overlap`` is neither dropped,
recreated nor altered — the overlap invariant stays practitioner-global on
purpose (§9 S1).
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

BOOTSTRAP_ORGANIZATION_ID = 1
BOOTSTRAP_ORGANIZATION_NAME = "Bootstrap Clinic"

# Every table that gains direct tenant ownership (PF0 T1 / §19.1 stage 2).
TENANT_OWNED_TABLES = (
    "services",
    "locations",
    "leads",
    "practitioner_capabilities",
    "availability_rules",
    "schedule_blocks",
    "appointments",
    "audit_events",
)

# Tenant-qualified referenced keys: the parents of §7.1's composite FK pattern.
ORGANIZATION_SCOPED_UNIQUE_KEYS = (
    ("locations", "uq_locations_organization_id"),
    ("services", "uq_services_organization_id"),
    ("leads", "uq_leads_organization_id"),
    ("appointments", "uq_appointments_organization_id"),
)

# (name, table, local columns, referenced table, referenced columns)
TENANT_COMPOSITE_FOREIGN_KEYS = (
    (
        "fk_leads_organization_service_need",
        "leads",
        ["organization_id", "service_need_id"],
        "services",
        ["organization_id", "id"],
    ),
    (
        "fk_capabilities_organization_membership",
        "practitioner_capabilities",
        ["organization_id", "practitioner_id"],
        "practitioner_memberships",
        ["organization_id", "practitioner_id"],
    ),
    (
        "fk_capabilities_organization_service",
        "practitioner_capabilities",
        ["organization_id", "service_id"],
        "services",
        ["organization_id", "id"],
    ),
    (
        "fk_capabilities_organization_location",
        "practitioner_capabilities",
        ["organization_id", "location_id"],
        "locations",
        ["organization_id", "id"],
    ),
    (
        "fk_availability_rules_organization_membership",
        "availability_rules",
        ["organization_id", "practitioner_id"],
        "practitioner_memberships",
        ["organization_id", "practitioner_id"],
    ),
    (
        "fk_availability_rules_organization_location",
        "availability_rules",
        ["organization_id", "location_id"],
        "locations",
        ["organization_id", "id"],
    ),
    (
        "fk_schedule_blocks_organization_membership",
        "schedule_blocks",
        ["organization_id", "practitioner_id"],
        "practitioner_memberships",
        ["organization_id", "practitioner_id"],
    ),
    (
        "fk_schedule_blocks_organization_location",
        "schedule_blocks",
        ["organization_id", "location_id"],
        "locations",
        ["organization_id", "id"],
    ),
    (
        "fk_appointments_organization_lead",
        "appointments",
        ["organization_id", "lead_id"],
        "leads",
        ["organization_id", "id"],
    ),
    (
        "fk_appointments_organization_service",
        "appointments",
        ["organization_id", "service_id"],
        "services",
        ["organization_id", "id"],
    ),
    (
        "fk_appointments_organization_membership",
        "appointments",
        ["organization_id", "practitioner_id"],
        "practitioner_memberships",
        ["organization_id", "practitioner_id"],
    ),
    (
        "fk_appointments_organization_location",
        "appointments",
        ["organization_id", "location_id"],
        "locations",
        ["organization_id", "id"],
    ),
)

# (index name, table, columns)
TENANT_INDEXES = (
    ("ix_audit_events_organization", "audit_events", ["organization_id", "occurred_at"]),
    (
        "ix_capabilities_organization_service_location",
        "practitioner_capabilities",
        ["organization_id", "service_id", "location_id"],
    ),
    (
        "ix_availability_rules_organization_location",
        "availability_rules",
        ["organization_id", "location_id"],
    ),
    (
        "ix_schedule_blocks_organization_location",
        "schedule_blocks",
        ["organization_id", "location_id"],
    ),
)


def upgrade() -> None:
    # --- stage 1: create and seed -------------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(length=250), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # The bootstrap tenant every Vertical 1 row belongs to. Its id is pinned so
    # the backfill below is deterministic and re-readable from application code
    # (``app.tenancy.BOOTSTRAP_ORGANIZATION_ID``); the identity sequence is then
    # advanced so the next organization does not collide with it.
    op.execute(
        sa.text(
            "INSERT INTO organizations (id, name) VALUES (:id, :name)"
        ).bindparams(id=BOOTSTRAP_ORGANIZATION_ID, name=BOOTSTRAP_ORGANIZATION_NAME)
    )
    op.execute(
        f"ALTER TABLE organizations ALTER COLUMN id RESTART WITH {BOOTSTRAP_ORGANIZATION_ID + 1}"
    )

    op.create_table(
        "practitioner_memberships",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("practitioner_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_practitioner_memberships_organization",
        ),
        sa.ForeignKeyConstraint(
            ["practitioner_id"],
            ["practitioners.id"],
            ondelete="RESTRICT",
            name="fk_practitioner_memberships_practitioner",
        ),
        # The natural tenant key referenced by every scheduling row that names a
        # practitioner (§7.1): the FK then *means* "works for this organization".
        sa.UniqueConstraint(
            "organization_id",
            "practitioner_id",
            name="uq_practitioner_memberships_org_practitioner",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_practitioner_memberships_org_id"
        ),
    )
    op.create_index(
        "ix_practitioner_memberships_practitioner",
        "practitioner_memberships",
        ["practitioner_id"],
    )

    # --- stage 2: add nullable tenant columns -------------------------------
    for table in TENANT_OWNED_TABLES:
        op.add_column(table, sa.Column("organization_id", sa.Integer(), nullable=True))

    # --- stage 3: backfill --------------------------------------------------
    # Exactly one organization exists, so nothing has to be guessed: every
    # existing row belongs to the single implicit tenant Vertical 1 always had
    # (§19.1 stage 3). Non-destructive: no row is deleted or rewritten beyond
    # gaining its tenant.
    for table in TENANT_OWNED_TABLES:
        op.execute(
            sa.text(
                f"UPDATE {table} SET organization_id = :org WHERE organization_id IS NULL"
            ).bindparams(org=BOOTSTRAP_ORGANIZATION_ID)
        )
    op.execute(
        sa.text(
            "INSERT INTO practitioner_memberships (organization_id, practitioner_id) "
            "SELECT :org, p.id FROM practitioners p"
        ).bindparams(org=BOOTSTRAP_ORGANIZATION_ID)
    )

    # --- stage 4: tighten ---------------------------------------------------
    for table in TENANT_OWNED_TABLES:
        op.alter_column(table, "organization_id", nullable=False)
        op.create_foreign_key(
            _organization_fk_name(table),
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    for table, name in ORGANIZATION_SCOPED_UNIQUE_KEYS:
        op.create_unique_constraint(name, table, ["organization_id", "id"])

    # The catalog becomes per tenant: two organizations may both sell the same
    # service name, one organization still may not list it twice.
    op.execute("ALTER TABLE services DROP CONSTRAINT IF EXISTS services_name_key")
    op.create_unique_constraint(
        "uq_services_organization_name", "services", ["organization_id", "name"]
    )

    for name, table, columns, referent, referent_columns in TENANT_COMPOSITE_FOREIGN_KEYS:
        # MATCH SIMPLE (PostgreSQL's default) is required, never MATCH FULL:
        # ``leads.service_need_id`` is nullable and must skip the check when the
        # optional service is absent (§7.3).
        op.create_foreign_key(
            name, table, referent, columns, referent_columns, ondelete="RESTRICT"
        )

    for name, table, columns in TENANT_INDEXES:
        op.create_index(name, table, columns)

    # --- stage 5: untouched -------------------------------------------------
    # excl_appointments_confirmed_no_overlap and
    # uq_capabilities_practitioner_service_location are deliberately not
    # mentioned above: neither is dropped, recreated or altered (§9 S1, PM7).


def downgrade() -> None:
    for name, table, _columns in reversed(TENANT_INDEXES):
        op.drop_index(name, table_name=table)

    for name, table, _columns, _referent, _referent_columns in reversed(
        TENANT_COMPOSITE_FOREIGN_KEYS
    ):
        op.drop_constraint(name, table, type_="foreignkey")

    op.drop_constraint("uq_services_organization_name", "services", type_="unique")
    op.create_unique_constraint("services_name_key", "services", ["name"])

    for table, name in reversed(ORGANIZATION_SCOPED_UNIQUE_KEYS):
        op.drop_constraint(name, table, type_="unique")

    for table in reversed(TENANT_OWNED_TABLES):
        op.drop_constraint(_organization_fk_name(table), table, type_="foreignkey")
        op.drop_column(table, "organization_id")

    op.drop_index(
        "ix_practitioner_memberships_practitioner",
        table_name="practitioner_memberships",
    )
    op.drop_table("practitioner_memberships")
    op.drop_table("organizations")


def _organization_fk_name(table: str) -> str:
    """The plain ``organization_id`` FK name for one tenant-owned table.

    ``practitioner_capabilities`` keeps the shorter ``capabilities`` prefix used
    by its Vertical 1 constraints.
    """
    prefix = "capabilities" if table == "practitioner_capabilities" else table
    return f"fk_{prefix}_organization"
