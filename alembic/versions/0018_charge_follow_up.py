"""Add deterministic charge collection follow-ups.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

PERMISSIONS = (
    ("follow_ups.read", "Read charge collection follow-ups"),
    ("follow_ups.create", "Open a charge collection follow-up"),
    ("follow_ups.manage", "Reschedule and close charge collection follow-ups"),
)


def upgrade() -> None:
    op.create_table(
        "charge_follow_ups",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("charge_id", sa.Integer(), nullable=False),
        sa.Column("next_follow_up_on", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("state", sa.String(length=10), nullable=False, server_default="open"),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=30), nullable=True),
        sa.CheckConstraint(
            "state IN ('open', 'closed')", name="ck_charge_follow_ups_state"
        ),
        sa.CheckConstraint(
            "close_reason IN ('settled', 'closed_by_operator')",
            name="ck_charge_follow_ups_close_reason",
        ),
        sa.CheckConstraint(
            "(state = 'open' AND closed_at IS NULL AND close_reason IS NULL) OR "
            "(state = 'closed' AND closed_at IS NOT NULL AND close_reason IS NOT NULL)",
            name="ck_charge_follow_ups_closure",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_charge_follow_ups_organization_id"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_charge_follow_ups_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "charge_id"],
            ["charges.organization_id", "charges.id"],
            ondelete="RESTRICT",
            name="fk_charge_follow_ups_organization_charge",
        ),
    )
    op.create_index(
        "uq_charge_follow_ups_org_charge_open",
        "charge_follow_ups",
        ["organization_id", "charge_id"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index(
        "ix_charge_follow_ups_org_due",
        "charge_follow_ups",
        ["organization_id", "next_follow_up_on"],
        postgresql_where=sa.text("state = 'open'"),
    )

    permission_table = sa.table(
        "permissions", sa.column("code", sa.String()), sa.column("name", sa.String())
    )
    op.bulk_insert(
        permission_table,
        [{"code": code, "name": name} for code, name in PERMISSIONS],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code = 'system' AND p.code = ANY(:codes)"
        ).bindparams(codes=[code for code, _name in PERMISSIONS])
    )


def downgrade() -> None:
    op.drop_index("ix_charge_follow_ups_org_due", table_name="charge_follow_ups")
    op.drop_index("uq_charge_follow_ups_org_charge_open", table_name="charge_follow_ups")
    op.drop_table("charge_follow_ups")
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = ANY(:codes)"
        ).bindparams(codes=[code for code, _name in PERMISSIONS])
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)").bindparams(
            codes=[code for code, _name in PERMISSIONS]
        )
    )
