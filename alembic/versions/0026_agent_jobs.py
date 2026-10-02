"""BACKFILL: ``agent_jobs`` (lease + fencing token), proposal kind ``waitlist_offer``
and ``agent_key='backfill'`` runs.

Additive: one new table plus widened ``ck_agent_proposals_kind`` and
``ck_agent_runs_agent_key``. The downgrade deletes the new rows first, drops the
table and restores the 0025 sets.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

KINDS_0025 = (
    "kind IN ('collection_reminder', 'collection_follow_up', 'inventory_transfer', "
    "'inventory_entry')"
)
KINDS_0026 = (
    "kind IN ('collection_reminder', 'collection_follow_up', 'inventory_transfer', "
    "'inventory_entry', 'waitlist_offer')"
)
AGENTS_0025 = "agent_key IN ('cobranza', 'reception', 'confirmaciones', 'inventario')"
AGENTS_0026 = (
    "agent_key IN ('cobranza', 'reception', 'confirmaciones', 'inventario', 'backfill')"
)


def upgrade() -> None:
    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("agent_key", sa.Text(), nullable=False),
        sa.Column("job_key", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("lease_token", UUID(as_uuid=True), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT",
                                name="fk_agent_jobs_organization"),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_event_id"],
            ["domain_events.organization_id", "domain_events.id"],
            ondelete="RESTRICT",
            name="fk_agent_jobs_organization_source_event",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_agent_jobs_organization_id"),
        sa.UniqueConstraint("organization_id", "job_key", name="uq_agent_jobs_organization_job_key"),
        sa.CheckConstraint("status IN ('queued', 'leased', 'done', 'failed', 'dead')",
                           name="ck_agent_jobs_status"),
        sa.CheckConstraint("agent_key IN ('backfill')", name="ck_agent_jobs_agent_key"),
        sa.CheckConstraint("attempts >= 0", name="ck_agent_jobs_attempts"),
        sa.CheckConstraint(
            "(status = 'leased') = (lease_token IS NOT NULL AND leased_until IS NOT NULL)",
            name="ck_agent_jobs_lease",
        ),
    )
    op.create_index(
        "ix_agent_jobs_org_due",
        "agent_jobs",
        ["organization_id", "run_after"],
        postgresql_where=sa.text("status IN ('queued', 'failed', 'leased')"),
    )
    op.drop_constraint("ck_agent_proposals_kind", "agent_proposals", type_="check")
    op.create_check_constraint("ck_agent_proposals_kind", "agent_proposals", KINDS_0026)
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_agent_key", "agent_runs", AGENTS_0026)


def downgrade() -> None:
    op.execute("DELETE FROM agent_proposals WHERE kind = 'waitlist_offer'")
    op.execute("DELETE FROM agent_runs WHERE agent_key = 'backfill'")
    op.drop_index("ix_agent_jobs_org_due", table_name="agent_jobs")
    op.drop_table("agent_jobs")
    op.drop_constraint("ck_agent_proposals_kind", "agent_proposals", type_="check")
    op.create_check_constraint("ck_agent_proposals_kind", "agent_proposals", KINDS_0025)
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_agent_key", "agent_runs", AGENTS_0025)
