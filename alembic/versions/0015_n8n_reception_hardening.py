"""Harden the synthetic reception boundary for n8n.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

RESUME_PERMISSION = (
    "conversations.resume",
    "Resume automation after a human receptionist resolves the handoff",
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_channel_accounts_provider", "channel_accounts", type_="check"
    )
    op.create_check_constraint(
        "ck_channel_accounts_provider",
        "channel_accounts",
        "provider IN ('whatsapp', 'test')",
    )

    permission_table = sa.table(
        "permissions", sa.column("code", sa.String()), sa.column("name", sa.String())
    )
    op.bulk_insert(
        permission_table,
        [{"code": RESUME_PERMISSION[0], "name": RESUME_PERMISSION[1]}],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code = 'system' AND p.code = :code"
        ).bindparams(code=RESUME_PERMISSION[0])
    )

    op.create_table(
        "appointment_cancellation_proposals",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("contact_identity_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=False),
        sa.Column(
            "confirmation_token", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("reason", sa.String(300)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            name="ck_appointment_cancellation_proposals_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_cancellation_proposals_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_cancellation_proposals_organization_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_cancellation_proposals_organization_contact",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "appointment_id"],
            ["appointments.organization_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_cancellation_proposals_organization_appointment",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id", "source_message_id"],
            ["messages.organization_id", "messages.conversation_id", "messages.id"],
            ondelete="RESTRICT",
            name="fk_cancellation_proposals_source_message",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_appointment_cancellation_proposals_organization_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "confirmation_token",
            name="uq_appointment_cancellation_proposals_confirmation_token",
        ),
    )
    op.create_index(
        "uq_cancellation_proposals_pending_appointment",
        "appointment_cancellation_proposals",
        ["organization_id", "conversation_id", "appointment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_cancellation_proposals_pending_appointment",
        table_name="appointment_cancellation_proposals",
    )
    op.drop_table("appointment_cancellation_proposals")
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = :code"
        ).bindparams(code=RESUME_PERMISSION[0])
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = :code").bindparams(
            code=RESUME_PERMISSION[0]
        )
    )
    op.drop_constraint(
        "ck_channel_accounts_provider", "channel_accounts", type_="check"
    )
    op.create_check_constraint(
        "ck_channel_accounts_provider",
        "channel_accounts",
        "provider IN ('whatsapp')",
    )
