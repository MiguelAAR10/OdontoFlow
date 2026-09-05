"""Complete deterministic receptionist tools and public catalog data.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

PERMISSIONS = (
    ("contact_profiles.manage", "Manage the patient profile bound to a channel contact"),
    ("contact_appointments.cancel", "Cancel appointments bound to a channel contact"),
    (
        "contact_appointments.reschedule",
        "Propose and confirm rescheduling bound to a channel contact",
    ),
    ("conversations.manage", "Request and manage human conversation handoff"),
)


def upgrade() -> None:
    permission_table = sa.table(
        "permissions", sa.column("code", sa.String()), sa.column("name", sa.String())
    )
    op.bulk_insert(
        permission_table,
        [{"code": code, "name": name} for code, name in PERMISSIONS],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code IN ('system', 'integration-conversation-agent') "
            "AND p.code = ANY(:codes)"
        ).bindparams(codes=[code for code, _name in PERMISSIONS])
    )

    op.add_column("services", sa.Column("public_description", sa.Text()))
    op.add_column("services", sa.Column("base_price", sa.Numeric(12, 2)))
    op.add_column(
        "services",
        sa.Column("currency", sa.String(3), nullable=False, server_default="PEN"),
    )
    op.add_column(
        "services",
        sa.Column(
            "booking_mode",
            sa.String(20),
            nullable=False,
            server_default="automatic",
        ),
    )
    op.create_check_constraint(
        "ck_services_nonnegative_base_price",
        "services",
        "base_price IS NULL OR base_price >= 0",
    )
    op.create_check_constraint(
        "ck_services_booking_mode",
        "services",
        "booking_mode IN ('automatic', 'evaluation_first', 'human_only')",
    )

    op.add_column("locations", sa.Column("address", sa.String(500)))
    op.add_column("locations", sa.Column("public_phone", sa.String(30)))
    op.add_column(
        "locations",
        sa.Column(
            "opening_hours",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("promotional_price", sa.Numeric(12, 2)),
        sa.Column("discount_percent", sa.Numeric(5, 2)),
        sa.Column("currency", sa.String(3), nullable=False, server_default="PEN"),
        sa.Column("service_id", sa.Integer()),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column(
            "new_patients_only", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("valid_until >= valid_from", name="ck_promotions_valid_dates"),
        sa.CheckConstraint(
            "promotional_price IS NULL OR promotional_price >= 0",
            name="ck_promotions_nonnegative_price",
        ),
        sa.CheckConstraint(
            "discount_percent IS NULL OR "
            "(discount_percent >= 0 AND discount_percent <= 100)",
            name="ck_promotions_discount_percent",
        ),
        sa.CheckConstraint("priority >= 0", name="ck_promotions_priority"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_promotions_organization_service",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_promotions_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id", "code", name="uq_promotions_organization_code"
        ),
    )

    op.add_column("appointments", sa.Column("patient_id", sa.Integer()))
    op.create_foreign_key(
        "fk_appointments_organization_patient",
        "appointments",
        "patients",
        ["organization_id", "patient_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.add_column("appointment_proposals", sa.Column("patient_id", sa.Integer()))
    op.create_foreign_key(
        "fk_appointment_proposals_organization_patient",
        "appointment_proposals",
        "patients",
        ["organization_id", "patient_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "appointment_reschedule_proposals",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("contact_identity_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("old_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("new_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("new_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "confirmation_token", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'expired')",
            name="ck_appointment_reschedule_proposals_status",
        ),
        sa.CheckConstraint(
            "old_end_utc > old_start_utc AND new_end_utc > new_start_utc",
            name="ck_appointment_reschedule_proposals_intervals",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_reschedule_proposals_organization_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_reschedule_proposals_organization_contact",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "appointment_id"],
            ["appointments.organization_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_reschedule_proposals_organization_appointment",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_reschedule_proposals_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "confirmation_token",
            name="uq_reschedule_proposals_confirmation_token",
        ),
    )
    op.create_index(
        "ix_reschedule_proposals_conversation_status",
        "appointment_reschedule_proposals",
        ["organization_id", "conversation_id", "status"],
    )

    op.create_table(
        "reception_handoffs",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("contact_identity_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=False),
        sa.Column("reason_summary", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "reason_code IN ('requested_by_contact', 'urgent_symptoms', 'complaint', "
            "'pricing_exception', 'clinical_case', 'low_confidence', 'other')",
            name="ck_reception_handoffs_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'resolved')",
            name="ck_reception_handoffs_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_reception_handoffs_organization_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_reception_handoffs_organization_contact",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_reception_handoffs_organization_id"
        ),
    )
    op.create_index(
        "uq_reception_handoffs_pending_conversation",
        "reception_handoffs",
        ["organization_id", "conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_reception_handoffs_pending_conversation",
        table_name="reception_handoffs",
    )
    op.drop_table("reception_handoffs")
    op.drop_index(
        "ix_reschedule_proposals_conversation_status",
        table_name="appointment_reschedule_proposals",
    )
    op.drop_table("appointment_reschedule_proposals")
    op.drop_constraint(
        "fk_appointment_proposals_organization_patient",
        "appointment_proposals",
        type_="foreignkey",
    )
    op.drop_column("appointment_proposals", "patient_id")
    op.drop_constraint(
        "fk_appointments_organization_patient", "appointments", type_="foreignkey"
    )
    op.drop_column("appointments", "patient_id")
    op.drop_table("promotions")
    op.drop_column("locations", "opening_hours")
    op.drop_column("locations", "public_phone")
    op.drop_column("locations", "address")
    op.drop_constraint("ck_services_booking_mode", "services", type_="check")
    op.drop_constraint(
        "ck_services_nonnegative_base_price", "services", type_="check"
    )
    op.drop_column("services", "booking_mode")
    op.drop_column("services", "currency")
    op.drop_column("services", "base_price")
    op.drop_column("services", "public_description")
    codes = [code for code, _name in PERMISSIONS]
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = ANY(:codes)"
        ).bindparams(codes=codes)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE code = ANY(:codes)").bindparams(codes=codes))


