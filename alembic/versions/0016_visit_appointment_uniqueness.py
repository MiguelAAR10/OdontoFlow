"""Guard one organization appointment against duplicate attendance.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_visits_org_appointment",
        "visits",
        ["organization_id", "appointment_id"],
        unique=True,
        postgresql_where=sa.text("appointment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_visits_org_appointment", table_name="visits")
