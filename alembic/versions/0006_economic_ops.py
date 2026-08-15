"""Economic & operations core: products, consumptions, charges, payments.

Implements the Economic & Operations Bridge (PF6): four organization-owned
tables with the §7 composite-FK pattern plus the eight new permission codes
and their grants to the seeded ``system`` role (PR7 pattern).

Strictly additive. Authority: ``.audit/clinical-core/next-economic-ops-contract.md``:
- Product declares its kind (CONSUMIBLE/REVENTA) — the legacy inferred
  distinction becomes declared (defect #6 dropped); no stock authority here.
- ServiceConsumption anchors to one execution line and one canonical product,
  ``UNIQUE(org, execution, product)`` (legacy ``UNIQUE(id_consulta_servicio,
  id_producto)``), positive quantity, unit-price snapshot.
- Charge is 1:1 per execution (legacy factura 1:1 adapted to the execution
  line) with a positive amount.
- Payment is N:1 per charge with positive amount; the overpayment guard lives
  in the application path (row lock), so no partial-DB constraint is needed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

PRODUCT_KIND_CHECK = "kind IN ('consumible', 'reventa')"

ECONOMIC_PERMISSION_CODES = (
    "products.read",
    "products.create",
    "consumptions.read",
    "consumptions.create",
    "charges.read",
    "charges.create",
    "payments.read",
    "payments.create",
)


def upgrade() -> None:
    # --- products -----------------------------------------------------------
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_products_organization",
        ),
        sa.CheckConstraint(PRODUCT_KIND_CHECK, name="ck_products_kind"),
        sa.UniqueConstraint("organization_id", "name", name="uq_products_organization_name"),
        sa.UniqueConstraint("organization_id", "id", name="uq_products_organization_id"),
    )

    # --- service_consumptions ----------------------------------------------
    op.create_table(
        "service_consumptions",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("service_execution_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_service_consumptions_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_execution_id"],
            ["service_executions.organization_id", "service_executions.id"],
            ondelete="RESTRICT",
            name="fk_service_consumptions_organization_execution",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "product_id"],
            ["products.organization_id", "products.id"],
            ondelete="RESTRICT",
            name="fk_service_consumptions_organization_product",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_service_consumptions_quantity"),
        sa.CheckConstraint("unit_price >= 0", name="ck_service_consumptions_price"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_service_consumptions_organization_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "service_execution_id",
            "product_id",
            name="uq_service_consumptions_org_execution_product",
        ),
    )

    # --- charges ------------------------------------------------------------
    op.create_table(
        "charges",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("service_execution_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_charges_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_execution_id"],
            ["service_executions.organization_id", "service_executions.id"],
            ondelete="RESTRICT",
            name="fk_charges_organization_execution",
        ),
        sa.CheckConstraint("amount > 0", name="ck_charges_amount"),
        sa.UniqueConstraint("organization_id", "id", name="uq_charges_organization_id"),
        sa.UniqueConstraint(
            "organization_id",
            "service_execution_id",
            name="uq_charges_org_execution",
        ),
    )

    # --- payments -----------------------------------------------------------
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("charge_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("method", sa.String(length=50), nullable=False),
        sa.Column(
            "paid_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_payments_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "charge_id"],
            ["charges.organization_id", "charges.id"],
            ondelete="RESTRICT",
            name="fk_payments_organization_charge",
        ),
        sa.CheckConstraint("amount > 0", name="ck_payments_amount"),
        sa.UniqueConstraint("organization_id", "id", name="uq_payments_organization_id"),
    )

    # --- permission codes + system grants (M5/PR7 pattern) ------------------
    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("code", sa.String),
            sa.column("name", sa.String),
        ),
        [
            {"code": code, "name": name}
            for code, name in (
                ("products.read", "Read products"),
                ("products.create", "Register products"),
                ("consumptions.read", "Read service consumptions"),
                ("consumptions.create", "Record service consumptions"),
                ("charges.read", "Read charges"),
                ("charges.create", "Register charges"),
                ("payments.read", "Read payments"),
                ("payments.create", "Record payments"),
            )
        ],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r "
            "CROSS JOIN permissions p "
            "WHERE r.code = :code AND p.code IN :codes"
        ).bindparams(
            sa.bindparam("code", value="system"),
            sa.bindparam("codes", value=tuple(ECONOMIC_PERMISSION_CODES), expanding=True),
        )
    )


def downgrade() -> None:
    codes = sa.bindparam("codes", value=tuple(ECONOMIC_PERMISSION_CODES), expanding=True)
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN ("
            "  SELECT id FROM permissions WHERE code IN :codes)"
        ).bindparams(codes)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(codes)
    )
    op.drop_table("payments")
    op.drop_table("charges")
    op.drop_table("service_consumptions")
    op.drop_table("products")
