"""Lead-to-Appointment persistence foundation.

Establishes the canonical domain model, the PostgreSQL ``btree_gist``
extension, and the partial GiST exclusion constraint that enforces the
booking invariant: two *confirmed* appointments cannot overlap for the
same practitioner (cancelled appointments never block interval reuse).
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(length=250), nullable=False, unique=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("duration_minutes > 0", name="ck_services_positive_duration"),
    )

    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(length=250), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "practitioners",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("display_name", sa.String(length=250), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "practitioner_capabilities",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("practitioner_id", sa.Integer(), sa.ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "practitioner_id",
            "service_id",
            "location_id",
            name="uq_capabilities_practitioner_service_location",
        ),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("contact_phone", sa.String(length=40)),
        sa.Column("contact_email", sa.String(length=255)),
        sa.Column("acquisition_source", sa.String(length=20), nullable=False),
        sa.Column("service_need_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="RESTRICT")),
        sa.Column("commercial_status", sa.String(length=30), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "acquisition_source IN ('promotion', 'referral', 'direct')",
            name="ck_leads_acquisition_source",
        ),
        sa.CheckConstraint(
            "contact_phone IS NOT NULL OR contact_email IS NOT NULL",
            name="ck_leads_at_least_one_contact",
        ),
    )

    op.create_table(
        "availability_rules",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("practitioner_id", sa.Integer(), sa.ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_local", sa.Time(), nullable=False),
        sa.Column("end_local", sa.Time(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_availability_rules_weekday"),
        sa.CheckConstraint("end_local > start_local", name="ck_availability_rules_interval"),
    )

    op.create_table(
        "schedule_blocks",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("practitioner_id", sa.Integer(), sa.ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("end_utc > start_utc", name="ck_schedule_blocks_interval"),
    )

    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("practitioner_id", sa.Integer(), sa.ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="confirmed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('confirmed', 'cancelled')", name="ck_appointments_state"),
        sa.CheckConstraint("end_utc > start_utc", name="ck_appointments_interval"),
        sa.dialects.postgresql.ExcludeConstraint(
            (sa.text("practitioner_id"), "="),
            (sa.text("tstzrange(start_utc, end_utc, '[)')"), "&&"),
            where=sa.text("state = 'confirmed'"),
            name="excl_appointments_confirmed_no_overlap",
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("before_state", sa.dialects.postgresql.JSONB()),
        sa.Column("after_state", sa.dialects.postgresql.JSONB()),
        sa.Column("correlation_id", sa.String(length=100)),
    )
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("appointments")
    op.drop_table("schedule_blocks")
    op.drop_table("availability_rules")
    op.drop_table("leads")
    op.drop_table("practitioner_capabilities")
    op.drop_table("practitioners")
    op.drop_table("locations")
    op.drop_table("services")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
