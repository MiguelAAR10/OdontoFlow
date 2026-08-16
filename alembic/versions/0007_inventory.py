"""Inventory ledger: inventory_movements + inventory permission codes.

Implements the Inventory vertical (PF7) per
``.audit/economic-ops/next-inventory-contract.md``:

- ``inventory_movements`` is an **append-only journal** — the single source of
  stock truth. No ``stock_actual`` column anywhere, no trigger cache: the
  balance is a derived read-time aggregate over the ledger
  (``Σ ENTRADA − Σ SALIDA + Σ signed ADJUSTMENT``).
- Movement types for V1: ``ENTRADA`` (purchase/initial input, the legacy SP
  adapted to a real HTTP surface), ``SALIDA`` (consumption-linked,
  ``id_consumo_origen UNIQUE`` keeps the 1:1 causal link), ``ADJUSTMENT``
  (reason-required correction — the legacy ``ajustar_stock`` defect is
  dropped: every stock change is a movement row with a reason).
- No location dimension (legacy has none); org-level stock only; transfers
  deferred.
- Per-type CHECKs: positive quantity for ENTRADA/SALIDA, non-zero for
  ADJUSTMENT; a reason is mandatory on adjustments.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

MOVEMENT_TYPE_CHECK = "type IN ('ENTRADA', 'SALIDA', 'ADJUSTMENT')"
QUANTITY_CHECK = (
    "(type IN ('ENTRADA', 'SALIDA') AND quantity > 0) "
    "OR (type = 'ADJUSTMENT' AND quantity <> 0)"
)
ADJUSTMENT_REASON_CHECK = "(type <> 'ADJUSTMENT') OR (reason IS NOT NULL AND reason <> '')"

INVENTORY_PERMISSION_CODES = (
    "movements.read",
    "movements.create",
)


def upgrade() -> None:
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        # 1:1 causal link to the consumption that produced a SALIDA.
        sa.Column("id_consumo_origen", sa.Integer(), nullable=True),
        sa.Column(
            "moved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_inventory_movements_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "product_id"],
            ["products.organization_id", "products.id"],
            ondelete="RESTRICT",
            name="fk_inventory_movements_organization_product",
        ),
        # MATCH SIMPLE: a movement without a consumption skips the check.
        sa.ForeignKeyConstraint(
            ["organization_id", "id_consumo_origen"],
            ["service_consumptions.organization_id", "service_consumptions.id"],
            ondelete="RESTRICT",
            name="fk_inventory_movements_organization_consumption",
        ),
        sa.CheckConstraint(MOVEMENT_TYPE_CHECK, name="ck_inventory_movements_type"),
        sa.CheckConstraint(QUANTITY_CHECK, name="ck_inventory_movements_quantity"),
        sa.CheckConstraint(ADJUSTMENT_REASON_CHECK, name="ck_inventory_movements_reason"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_inventory_movements_organization_id",
        ),
        sa.UniqueConstraint(
            "id_consumo_origen",
            name="uq_inventory_movements_consumo_origen",
        ),
    )
    op.create_index(
        "ix_inventory_movements_org_product",
        "inventory_movements",
        ["organization_id", "product_id", "id"],
    )

    # DB-level causality: a consumption-linked SALIDA must reference the same
    # product the consumption used. The application always writes it
    # correctly; this trigger makes a mismatched pairing structurally
    # impossible (the composite FK alone cannot compare across tables).
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION trg_inventory_movements_salida_product()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.type = 'SALIDA' AND NEW.id_consumo_origen IS NOT NULL THEN
                    IF NEW.product_id <> (
                        SELECT product_id FROM service_consumptions
                        WHERE id = NEW.id_consumo_origen
                    ) THEN
                        RAISE EXCEPTION 'SALIDA product must match the consumption product';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_inventory_movements_salida_product
            BEFORE INSERT OR UPDATE ON inventory_movements
            FOR EACH ROW EXECUTE FUNCTION trg_inventory_movements_salida_product();
            """
        )
    )

    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("code", sa.String),
            sa.column("name", sa.String),
        ),
        [
            {"code": "movements.read", "name": "Read inventory movements"},
            {"code": "movements.create", "name": "Record inventory movements"},
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
            sa.bindparam("codes", value=tuple(INVENTORY_PERMISSION_CODES), expanding=True),
        )
    )


def downgrade() -> None:
    codes = sa.bindparam("codes", value=tuple(INVENTORY_PERMISSION_CODES), expanding=True)
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN ("
            "  SELECT id FROM permissions WHERE code IN :codes)"
        ).bindparams(codes)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(codes)
    )
    op.drop_index("ix_inventory_movements_org_product", table_name="inventory_movements")
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_inventory_movements_salida_product ON inventory_movements")
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS trg_inventory_movements_salida_product()")
    )
    op.drop_table("inventory_movements")
