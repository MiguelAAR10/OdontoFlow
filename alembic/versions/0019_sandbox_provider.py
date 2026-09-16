"""Admit the development-only local sandbox provider.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

PROVIDERS = "provider IN ('whatsapp', 'test', 'sandbox')"
PRIOR_PROVIDERS = "provider IN ('whatsapp', 'test')"


def upgrade() -> None:
    op.drop_constraint(
        "ck_channel_accounts_provider", "channel_accounts", type_="check"
    )
    op.create_check_constraint(
        "ck_channel_accounts_provider", "channel_accounts", PROVIDERS
    )
    op.create_unique_constraint(
        "uq_outbound_messages_organization_id", "outbound_messages", ["organization_id", "id"]
    )
    op.create_table(
        "sandbox_delivery_receipts",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("outbound_id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(255), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "provider_message_id LIKE 'sandbox-%'",
            name="ck_sandbox_delivery_receipts_provider_message_id",
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_sandbox_delivery_receipts_payload_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_sandbox_delivery_receipts_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "outbound_id"],
            ["outbound_messages.organization_id", "outbound_messages.id"],
            ondelete="RESTRICT",
            name="fk_sandbox_delivery_receipts_organization_outbound",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "outbound_id",
            name="uq_sandbox_delivery_receipts_organization_outbound",
        ),
        sa.UniqueConstraint(
            "provider_message_id",
            name="uq_sandbox_delivery_receipts_provider_message_id",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    sandbox_rows = connection.execute(
        sa.text("SELECT 1 FROM channel_accounts WHERE provider = 'sandbox' LIMIT 1")
    ).first()
    receipt_rows = connection.execute(
        sa.text("SELECT 1 FROM sandbox_delivery_receipts LIMIT 1")
    ).first()
    if sandbox_rows is not None or receipt_rows is not None:
        raise RuntimeError(
            "Cannot downgrade while sandbox channel accounts or delivery receipts still exist."
        )
    op.drop_table("sandbox_delivery_receipts")
    op.drop_constraint(
        "uq_outbound_messages_organization_id", "outbound_messages", type_="unique"
    )
    op.drop_constraint(
        "ck_channel_accounts_provider", "channel_accounts", type_="check"
    )
    op.create_check_constraint(
        "ck_channel_accounts_provider", "channel_accounts", PRIOR_PROVIDERS
    )
