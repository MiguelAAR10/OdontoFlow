"""COB minimal agent runs (one row per 'ejecutar ahora' sweep).

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("agent_key", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("triggered_by_principal_id", sa.Integer(), nullable=False),
        sa.Column("candidates_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proposed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deduped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("agent_key IN ('cobranza')", name="ck_agent_runs_agent_key"),
        sa.CheckConstraint(
            "trigger IN ('manual', 'schedule', 'event')", name="ck_agent_runs_trigger"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name="ck_agent_runs_status"
        ),
        sa.CheckConstraint(
            "candidates_count >= 0 AND proposed_count >= 0 AND deduped_count >= 0 "
            "AND skipped_count >= 0",
            name="ck_agent_runs_counts_non_negative",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL) "
            "AND (finished_at IS NULL OR finished_at >= started_at)",
            name="ck_agent_runs_finished",
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_category IS NOT NULL)", name="ck_agent_runs_error"
        ),
        sa.CheckConstraint(
            "status <> 'completed' "
            "OR proposed_count + deduped_count + skipped_count = candidates_count",
            name="ck_agent_runs_counts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_agent_runs_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "triggered_by_principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_agent_runs_organization_triggered_by",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_agent_runs_organization_id"),
    )
    op.create_index(
        "ix_agent_runs_org_agent_started",
        "agent_runs",
        ["organization_id", "agent_key", sa.text("started_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_org_agent_started", table_name="agent_runs")
    op.drop_table("agent_runs")
