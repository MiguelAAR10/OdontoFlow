"""INV inventory agent: proposal kinds ``inventory_transfer``/``inventory_entry``
and ``agent_key='inventario'`` runs.

Additive: only widens ``ck_agent_proposals_kind`` and ``ck_agent_runs_agent_key``.
The downgrade deletes those rows first and restores the 0024 sets.

Revision ID: 0025
Revises: 0024
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_proposals_kind", "agent_proposals", type_="check")
    op.create_check_constraint(
        "ck_agent_proposals_kind",
        "agent_proposals",
        "kind IN ('collection_reminder', 'collection_follow_up', 'inventory_transfer', "
        "'inventory_entry')",
    )
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_agent_key",
        "agent_runs",
        "agent_key IN ('cobranza', 'reception', 'confirmaciones', 'inventario')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM agent_proposals WHERE kind IN ('inventory_transfer', 'inventory_entry')"
    )
    op.execute("DELETE FROM agent_runs WHERE agent_key = 'inventario'")
    op.drop_constraint("ck_agent_proposals_kind", "agent_proposals", type_="check")
    op.create_check_constraint(
        "ck_agent_proposals_kind",
        "agent_proposals",
        "kind IN ('collection_reminder', 'collection_follow_up')",
    )
    op.drop_constraint("ck_agent_runs_agent_key", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_agent_key",
        "agent_runs",
        "agent_key IN ('cobranza', 'reception', 'confirmaciones')",
    )
