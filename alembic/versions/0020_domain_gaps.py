"""B0.5 domain gaps: appointment outcomes, payment reversals, reorder points,
waitlist and domain events.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

APPOINTMENT_STATES = "state IN ('confirmed', 'cancelled', 'completed', 'no_show')"
PRIOR_APPOINTMENT_STATES = "state IN ('confirmed', 'cancelled')"

PERMISSIONS = (
    ("appointments.record_outcome", "Mark appointments completed or no-show"),
    ("payments.reverse", "Reverse a recorded payment in full"),
    ("reorder_points.manage", "Set inventory reorder points per product and location"),
    ("waitlist.read", "Read the appointment waitlist"),
    ("waitlist.manage", "Add and cancel appointment waitlist entries"),
)
CODES = [code for code, _name in PERMISSIONS]


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_organization",
    )


def _tenant_fk(table: str, column: str, target: str, suffix: str, target_column: str = "id"):
    return sa.ForeignKeyConstraint(
        ["organization_id", column],
        [f"{target}.organization_id", f"{target}.{target_column}"],
        ondelete="RESTRICT",
        name=f"fk_{table}_organization_{suffix}",
    )


def upgrade() -> None:
    # 1. Appointment outcomes. The GiST exclusion (confirmed only) is untouched.
    op.drop_constraint("ck_appointments_state", "appointments", type_="check")
    op.create_check_constraint("ck_appointments_state", "appointments", APPOINTMENT_STATES)

    # 2. Domain events (internal outbox, same transaction as the change).
    op.create_table(
        "domain_events",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        _organization_fk("domain_events"),
        sa.UniqueConstraint("organization_id", "id", name="uq_domain_events_organization_id"),
    )
    op.create_index(
        "ix_domain_events_org_type_id", "domain_events", ["organization_id", "event_type", "id"]
    )

    # 3. Payment reversals: one full reversal per payment; payments untouched.
    op.create_table(
        "payment_reversals",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "reversed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by_principal_id", sa.Integer(), nullable=False),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_payment_reversals_reason"),
        _organization_fk("payment_reversals"),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            ondelete="RESTRICT",
            name="fk_payment_reversals_principal",
        ),
        _tenant_fk("payment_reversals", "payment_id", "payments", "payment"),
        sa.UniqueConstraint("organization_id", "id", name="uq_payment_reversals_organization_id"),
        sa.UniqueConstraint(
            "organization_id", "payment_id", name="uq_payment_reversals_org_payment"
        ),
    )

    # 4. Reorder points per product × location.
    op.create_table(
        "reorder_points",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("min_quantity", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("min_quantity >= 0", name="ck_reorder_points_min_quantity"),
        _organization_fk("reorder_points"),
        _tenant_fk("reorder_points", "product_id", "products", "product"),
        _tenant_fk("reorder_points", "location_id", "locations", "location"),
        sa.UniqueConstraint("organization_id", "id", name="uq_reorder_points_organization_id"),
        sa.UniqueConstraint(
            "organization_id",
            "product_id",
            "location_id",
            name="uq_reorder_points_org_product_location",
        ),
    )

    # 5. Minimal waitlist.
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("practitioner_id", sa.Integer(), nullable=True),
        sa.Column("earliest_date", sa.Date(), nullable=False),
        sa.Column("latest_date", sa.Date(), nullable=False),
        sa.Column("preferred_window", sa.String(length=10), nullable=False, server_default="any"),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="open"),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("latest_date >= earliest_date", name="ck_waitlist_entries_dates"),
        sa.CheckConstraint(
            "preferred_window IN ('any', 'morning', 'afternoon')",
            name="ck_waitlist_entries_window",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'offered', 'booked', 'cancelled', 'expired')",
            name="ck_waitlist_entries_status",
        ),
        _organization_fk("waitlist_entries"),
        _tenant_fk("waitlist_entries", "lead_id", "leads", "lead"),
        _tenant_fk("waitlist_entries", "patient_id", "patients", "patient"),
        _tenant_fk("waitlist_entries", "service_id", "services", "service"),
        _tenant_fk("waitlist_entries", "location_id", "locations", "location"),
        _tenant_fk(
            "waitlist_entries",
            "practitioner_id",
            "practitioner_memberships",
            "membership",
            target_column="practitioner_id",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_waitlist_entries_organization_id"),
    )
    op.create_index(
        "ix_waitlist_entries_org_status_service",
        "waitlist_entries",
        ["organization_id", "status", "service_id"],
    )

    # 6. Permissions, granted to the seeded system role (pattern 0018).
    permission_table = sa.table(
        "permissions", sa.column("code", sa.String()), sa.column("name", sa.String())
    )
    op.bulk_insert(permission_table, [{"code": code, "name": name} for code, name in PERMISSIONS])
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code = 'system' AND p.code = ANY(:codes)"
        ).bindparams(codes=CODES)
    )


def downgrade() -> None:
    # Refuse cleanly — before touching anything — while outcome states or
    # reversals exist: the prior CHECK cannot hold the former, and dropping the
    # latter would silently turn reversed payments back into paid charges. The
    # downgrade never rewrites data.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM appointments WHERE state IN ('completed', 'no_show')) THEN "
        "RAISE EXCEPTION 'downgrade 0020: hay citas completed/no_show; "
        "resuélvalas antes de bajar la migración'; "
        "END IF; "
        "IF EXISTS (SELECT 1 FROM payment_reversals) THEN "
        "RAISE EXCEPTION 'downgrade 0020: hay filas en payment_reversals; "
        "borrarlas devolvería pagos revertidos a cobros pagados'; "
        "END IF; END $$;"
    )
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = ANY(:codes)"
        ).bindparams(codes=CODES)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)").bindparams(codes=CODES)
    )
    op.drop_index("ix_waitlist_entries_org_status_service", table_name="waitlist_entries")
    op.drop_table("waitlist_entries")
    op.drop_table("reorder_points")
    op.drop_table("payment_reversals")
    op.drop_index("ix_domain_events_org_type_id", table_name="domain_events")
    op.drop_table("domain_events")
    op.drop_constraint("ck_appointments_state", "appointments", type_="check")
    op.create_check_constraint("ck_appointments_state", "appointments", PRIOR_APPOINTMENT_STATES)
