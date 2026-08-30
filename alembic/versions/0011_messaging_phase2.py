"""Durable conversations, messages and PostgreSQL outbound delivery queue.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

MESSAGING_PERMISSIONS = (
    ("messages.create", "Ingest normalized channel messages"),
    ("deliveries.create", "Queue outbound channel deliveries"),
    ("deliveries.manage", "Claim and settle outbound deliveries"),
    ("conversations.read", "Read channel conversations"),
)


def upgrade() -> None:
    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("external_account_id", sa.String(128), nullable=False),
        sa.Column("phone_number_id", sa.String(128)),
        sa.Column("display_name", sa.String(150), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "provider IN ('whatsapp')", name="ck_channel_accounts_provider"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_channel_accounts_organization",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_channel_accounts_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            "external_account_id",
            name="uq_channel_accounts_provider_external",
        ),
    )
    op.create_index(
        "uq_channel_accounts_phone_number",
        "channel_accounts",
        ["organization_id", "provider", "phone_number_id"],
        unique=True,
        postgresql_where=sa.text("phone_number_id IS NOT NULL"),
    )

    op.create_table(
        "contact_identities",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("channel_account_id", sa.Integer(), nullable=False),
        sa.Column("external_contact_id", sa.String(255), nullable=False),
        sa.Column("normalized_phone_e164", sa.String(16), nullable=False),
        sa.Column("lead_id", sa.Integer()),
        sa.Column("patient_id", sa.Integer()),
        sa.Column(
            "consent_status",
            sa.String(20),
            nullable=False,
            server_default="unknown",
        ),
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
            "consent_status IN ('unknown', 'pending', 'opted_in', 'opted_out')",
            name="ck_contact_identities_consent_status",
        ),
        sa.CheckConstraint(
            "normalized_phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_contact_identities_phone_e164",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "channel_account_id"],
            ["channel_accounts.organization_id", "channel_accounts.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization_channel",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization_lead",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "patient_id"],
            ["patients.organization_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization_patient",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_contact_identities_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "channel_account_id",
            "external_contact_id",
            name="uq_contact_identities_channel_external",
        ),
    )
    op.create_index(
        "ix_contact_identities_org_phone",
        "contact_identities",
        ["organization_id", "normalized_phone_e164"],
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("channel_account_id", sa.Integer(), nullable=False),
        sa.Column("contact_identity_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
        sa.Column("assigned_principal_id", sa.Integer()),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
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
            "status IN ('open', 'awaiting_confirmation', 'human_handoff', 'closed')",
            name="ck_conversations_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "channel_account_id"],
            ["channel_accounts.organization_id", "channel_accounts.id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization_channel",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization_contact",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "assigned_principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization_assignee",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_conversations_organization_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "channel_account_id",
            "id",
            name="uq_conversations_organization_channel_id",
        ),
    )
    op.create_index(
        "uq_conversations_active_contact",
        "conversations",
        ["organization_id", "channel_account_id", "contact_identity_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.create_index(
        "ix_conversations_org_last_message",
        "conversations",
        ["organization_id", "last_message_at"],
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("channel_account_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("message_type", sa.String(20), nullable=False),
        sa.Column("body_text", sa.Text()),
        sa.Column("media_reference", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("delivery_status", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_redacted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_messages_direction"
        ),
        sa.CheckConstraint(
            "message_type IN ('text', 'audio', 'image', 'template', 'system')",
            name="ck_messages_type",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('received', 'pending', 'processing', 'sent', "
            "'delivered', 'read', 'failed', 'dead_letter')",
            name="ck_messages_delivery_status",
        ),
        sa.CheckConstraint(
            "direction <> 'inbound' OR provider_message_id IS NOT NULL",
            name="ck_messages_inbound_provider_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_messages_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "channel_account_id", "conversation_id"],
            [
                "conversations.organization_id",
                "conversations.channel_account_id",
                "conversations.id",
            ],
            ondelete="RESTRICT",
            name="fk_messages_organization_channel_conversation",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_messages_organization_id"),
        sa.UniqueConstraint(
            "organization_id",
            "conversation_id",
            "id",
            name="uq_messages_organization_conversation_id",
        ),
    )
    op.create_index(
        "uq_messages_provider_id",
        "messages",
        ["organization_id", "channel_account_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_messages_conversation_occurred",
        "messages",
        ["conversation_id", "occurred_at"],
    )
    op.create_index("ix_messages_content_expiry", "messages", ["content_expires_at"])

    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(36), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_result_idempotency_key", sa.String(36)),
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
            "status IN ('pending', 'processing', 'sent', 'delivered', 'failed', "
            "'dead_letter')",
            name="ck_outbound_messages_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_outbound_messages_attempt_count"
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            "[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
            name="ck_outbound_messages_idempotency_uuid4",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_outbound_messages_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id", "message_id"],
            ["messages.organization_id", "messages.conversation_id", "messages.id"],
            ondelete="RESTRICT",
            name="fk_outbound_messages_organization_message",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_outbound_messages_organization_idempotency",
        ),
    )
    op.create_index(
        "ix_outbound_messages_dispatch",
        "outbound_messages",
        ["organization_id", "status", "next_attempt_at"],
    )

    permission_table = sa.table(
        "permissions",
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    op.bulk_insert(
        permission_table,
        [{"code": code, "name": name} for code, name in MESSAGING_PERMISSIONS],
    )
    codes = ", ".join(f"'{code}'" for code, _ in MESSAGING_PERMISSIONS)
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            f"WHERE r.code = 'system' AND p.code IN ({codes})"
        )
    )


def downgrade() -> None:
    codes = ", ".join(f"'{code}'" for code, _ in MESSAGING_PERMISSIONS)
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            f"WHERE rp.permission_id = p.id AND p.code IN ({codes})"
        )
    )
    op.execute(sa.text(f"DELETE FROM permissions WHERE code IN ({codes})"))
    op.drop_table("outbound_messages")
    op.drop_index("ix_messages_content_expiry", table_name="messages")
    op.drop_index("ix_messages_conversation_occurred", table_name="messages")
    op.drop_index("uq_messages_provider_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_org_last_message", table_name="conversations")
    op.drop_index("uq_conversations_active_contact", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_contact_identities_org_phone", table_name="contact_identities")
    op.drop_table("contact_identities")
    op.drop_index("uq_channel_accounts_phone_number", table_name="channel_accounts")
    op.drop_table("channel_accounts")

