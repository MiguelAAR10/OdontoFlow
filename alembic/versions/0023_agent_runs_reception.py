"""B3 reception turn runs: ``agent_key='reception'`` rows bound to a message.

Additive: widens ``ck_agent_runs_agent_key`` and adds two nullable columns with
one composite FK into ``messages(organization_id, conversation_id, id)``, so a
run can only cite a message of that conversation in that tenant (MATCH SIMPLE:
COB rows leave both null). No index: the activity feed reads ``agent_runs`` by
``organization_id`` (``ix_agent_runs_org_agent_started``).

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_agent_key", "agent_runs", "agent_key IN ('cobranza', 'reception')"
    )
    op.add_column("agent_runs", sa.Column("conversation_id", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("trigger_message_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_agent_runs_organization_trigger_message",
        "agent_runs",
        "messages",
        ["organization_id", "conversation_id", "trigger_message_id"],
        ["organization_id", "conversation_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_agent_runs_reception_trigger",
        "agent_runs",
        "agent_key <> 'reception' OR (trigger = 'event' AND conversation_id IS NOT NULL "
        "AND trigger_message_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_runs WHERE agent_key = 'reception'")
    op.drop_constraint("ck_agent_runs_reception_trigger", "agent_runs", type_="check")
    op.drop_constraint(
        "fk_agent_runs_organization_trigger_message", "agent_runs", type_="foreignkey"
    )
    op.drop_column("agent_runs", "trigger_message_id")
    op.drop_column("agent_runs", "conversation_id")
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_agent_key", "agent_runs", "agent_key IN ('cobranza')"
    )
