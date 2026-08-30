"""Narrow contact-bound appointment permission for reception tools.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

PERMISSION_CODE = "contact_appointments.read"
PERMISSION_NAME = "Read appointments bound to a channel contact"


def upgrade() -> None:
    permission_table = sa.table(
        "permissions",
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    op.bulk_insert(
        permission_table,
        [{"code": PERMISSION_CODE, "name": PERMISSION_NAME}],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code = 'system' AND p.code = :code"
        ).bindparams(code=PERMISSION_CODE)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = :code"
        ).bindparams(code=PERMISSION_CODE)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = :code").bindparams(
            code=PERMISSION_CODE
        )
    )

