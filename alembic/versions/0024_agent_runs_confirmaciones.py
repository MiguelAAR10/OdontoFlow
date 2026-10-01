"""SELF D-1 reminder runs: ``agent_key='confirmaciones'``.

Additive: only widens ``ck_agent_runs_agent_key``. A confirmaciones run is a
manual sweep like COB, so no other CHECK changes; the downgrade deletes those
rows first and restores 0023's set.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_agent_key",
        "agent_runs",
        "agent_key IN ('cobranza', 'reception', 'confirmaciones')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_runs WHERE agent_key = 'confirmaciones'")
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_agent_key", "agent_runs", "agent_key IN ('cobranza', 'reception')"
    )
