"""Close the external HTTP security boundary.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("request_id", sa.String(36)))

    op.create_table(
        "security_events",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer()),
        sa.Column("principal_id", sa.Integer()),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'blocked')",
            name="ck_security_events_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_security_events_organization",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            ondelete="RESTRICT",
            name="fk_security_events_principal",
        ),
    )
    op.create_index(
        "ix_security_events_occurred_at", "security_events", ["occurred_at"]
    )
    op.create_index(
        "ix_security_events_organization",
        "security_events",
        ["organization_id", "occurred_at"],
    )

    op.create_table(
        "integration_rate_limits",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("credential_id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "category IN ('read', 'mutation')",
            name="ck_integration_rate_limits_category",
        ),
        sa.CheckConstraint(
            "request_count > 0",
            name="ck_integration_rate_limits_positive_count",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["integration_credentials.id"],
            ondelete="RESTRICT",
            name="fk_integration_rate_limits_credential",
        ),
        sa.UniqueConstraint(
            "credential_id",
            "window_start",
            "category",
            name="uq_integration_rate_limits_window",
        ),
    )


def downgrade() -> None:
    op.drop_table("integration_rate_limits")
    op.drop_index("ix_security_events_organization", table_name="security_events")
    op.drop_index("ix_security_events_occurred_at", table_name="security_events")
    op.drop_table("security_events")
    op.drop_column("audit_events", "request_id")

