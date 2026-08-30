"""Persist contact-bound appointment proposals and booking permission.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

PERMISSION_CODE = "contact_appointments.book"
PERMISSION_NAME = "Propose and confirm appointments bound to a channel contact"


def upgrade() -> None:
    permission_table = sa.table(
        "permissions",
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    op.bulk_insert(
        permission_table,
        [{"code": PERMISSION_CODE, "name": PERMISSION_NAME}],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code IN ('system', 'integration-conversation-agent') "
            "AND p.code = :code"
        ).bindparams(code=PERMISSION_CODE)
    )

    op.create_table(
        "appointment_proposals",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("contact_identity_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("practitioner_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "confirmation_token", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'expired')",
            name="ck_appointment_proposals_status",
        ),
        sa.CheckConstraint(
            "end_utc > start_utc", name="ck_appointment_proposals_interval"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_contact",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_lead",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_service",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            [
                "practitioner_memberships.organization_id",
                "practitioner_memberships.practitioner_id",
            ],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_membership",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_location",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "appointment_id"],
            ["appointments.organization_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_appointment_proposals_organization_appointment",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_appointment_proposals_organization_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "confirmation_token",
            name="uq_appointment_proposals_confirmation_token",
        ),
    )
    op.create_index(
        "ix_appointment_proposals_conversation_status",
        "appointment_proposals",
        ["organization_id", "conversation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_appointment_proposals_conversation_status",
        table_name="appointment_proposals",
    )
    op.drop_table("appointment_proposals")
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = :code"
        ).bindparams(code=PERMISSION_CODE)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = :code").bindparams(
            code=PERMISSION_CODE
        )
    )

