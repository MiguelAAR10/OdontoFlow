"""Add typed payment methods and reconciliation metadata.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

PERMISSION = ("payments.manage", "Verify and reconcile recorded payments")


def upgrade() -> None:
    # Normalize the historical display labels before adding the closed method
    # vocabulary.  The migration intentionally has no fallback value: an
    # unknown historical method must make the migration fail loudly.
    op.execute(
        sa.text(
            "UPDATE payments SET method = CASE lower(method) "
            "WHEN 'efectivo' THEN 'efectivo' "
            "WHEN 'tarjeta' THEN 'tarjeta' "
            "WHEN 'yape' THEN 'yape' "
            "WHEN 'plin' THEN 'plin' "
            "WHEN 'transferencia' THEN 'transferencia' "
            "WHEN 'link de pago' THEN 'link_pago' "
            "WHEN 'link_pago' THEN 'link_pago' "
            "ELSE method END"
        )
    )
    op.create_check_constraint(
        "ck_payments_method",
        "payments",
        "method IN ('efectivo', 'tarjeta', 'yape', 'plin', 'transferencia', 'link_pago')",
    )
    op.add_column("payments", sa.Column("reference", sa.String(length=60), nullable=True))
    op.add_column("payments", sa.Column("receiver", sa.String(length=120), nullable=True))
    op.add_column(
        "payments", sa.Column("reconciliation_note", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "payments", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payments",
        sa.Column(
            "verification_status",
            sa.String(length=20),
            nullable=False,
            server_default="unverified",
        ),
    )
    op.create_check_constraint(
        "ck_payments_verification_status",
        "payments",
        "verification_status IN ('unverified', 'verified')",
    )
    op.create_check_constraint(
        "ck_payments_verified_at_consistency",
        "payments",
        "(verification_status = 'unverified' AND verified_at IS NULL) OR "
        "(verification_status = 'verified' AND verified_at IS NOT NULL)",
    )
    # Existing digital rows can be historical and have no operation code.  The
    # NOT VALID constraint preserves those rows while enforcing all new writes.
    op.execute(
        sa.text(
            "ALTER TABLE payments ADD CONSTRAINT ck_payments_digital_reference "
            "CHECK (method NOT IN ('yape', 'plin', 'transferencia') "
            "OR reference IS NOT NULL) NOT VALID"
        )
    )
    op.create_index(
        "uq_payments_org_method_reference",
        "payments",
        ["organization_id", "method", "reference"],
        unique=True,
        postgresql_where=sa.text("reference IS NOT NULL"),
    )

    permission_table = sa.table(
        "permissions", sa.column("code", sa.String()), sa.column("name", sa.String())
    )
    op.bulk_insert(permission_table, [{"code": PERMISSION[0], "name": PERMISSION[1]}])
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code = 'system' AND p.code = :code"
        ).bindparams(code=PERMISSION[0])
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = :code"
        ).bindparams(code=PERMISSION[0])
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = :code").bindparams(code=PERMISSION[0])
    )
    op.drop_index("uq_payments_org_method_reference", table_name="payments")
    op.drop_constraint("ck_payments_digital_reference", "payments", type_="check")
    op.drop_constraint(
        "ck_payments_verified_at_consistency", "payments", type_="check"
    )
    op.drop_constraint("ck_payments_verification_status", "payments", type_="check")
    op.drop_column("payments", "verification_status")
    op.drop_column("payments", "verified_at")
    op.drop_column("payments", "reconciliation_note")
    op.drop_column("payments", "receiver")
    op.drop_column("payments", "reference")
    op.drop_constraint("ck_payments_method", "payments", type_="check")
    op.execute(
        sa.text(
            "UPDATE payments SET method = CASE method "
            "WHEN 'efectivo' THEN 'Efectivo' "
            "WHEN 'tarjeta' THEN 'Tarjeta' "
            "WHEN 'yape' THEN 'Yape' "
            "WHEN 'plin' THEN 'Plin' "
            "WHEN 'transferencia' THEN 'Transferencia' "
            "WHEN 'link_pago' THEN 'Link de pago' "
            "ELSE method END"
        )
    )
