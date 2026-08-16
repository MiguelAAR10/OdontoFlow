"""M4.2 — location-aware inventory: location_id + transfers on the ledger.

Extends the PF7 inventory truth from Product × Organization to
Product × Location WITHOUT introducing a second stock authority:
``inventory_movements`` stays the only truth; the balance is still a derived
read-time aggregate, now per ``(organization_id, product_id, location_id)``.

What this migration does:

1. **``location_id`` on every movement** — composite FK
   ``(organization_id, location_id) → locations(organization_id, id)``
   (RESTRICT), so a movement can never point at another organization's
   location; the existing composite product FK already pins the product to
   the same organization.
2. **Staged backfill that never fabricates.** Existing org-level rows have no
   truthful location. The only determinable rows are consumption-linked
   SALIDAs (``id_consumo_origen``), whose location is recovered from their
   consumption → execution → visit chain. Any row still without a location
   after the backfill (org-level ENTRADA/ADJUSTMENT) aborts the upgrade with
   an explicit error — the operator must assign it explicitly. (Verified at
   upgrade time: no environment currently holds ledger rows — dev is at 0001,
   test is reset per run — so this guard is the safety net, not the norm.)
3. **Transfers** — two new movement types ``TRANSFER_OUT`` / ``TRANSFER_IN``
   linked by a shared ``transfer_id`` (String(36), server-generated UUID).
   Exactly-one-Out and exactly-one-In per transfer_id are partial unique
   indexes; the pairing invariants (same organization, product and quantity;
   distinct locations; TRANSFER rows always carry a transfer_id; non-transfer
   rows never do) are enforced by a deferred constraint trigger validated at
   COMMIT — the pair lands atomically or not at all.
4. Index for the balance query: ``(organization_id, product_id, location_id,
   id)``.

No new stock column, no trigger cache, no second writer — the ledger remains
the single authority.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

MOVEMENT_TYPE_CHECK = (
    "type IN ('ENTRADA', 'SALIDA', 'ADJUSTMENT', 'TRANSFER_OUT', 'TRANSFER_IN')"
)
QUANTITY_CHECK = (
    "(type IN ('ENTRADA', 'SALIDA', 'TRANSFER_OUT', 'TRANSFER_IN') AND quantity > 0) "
    "OR (type = 'ADJUSTMENT' AND quantity <> 0)"
)
V1_TYPE_CHECK = "type IN ('ENTRADA', 'SALIDA', 'ADJUSTMENT')"
V1_QUANTITY_CHECK = (
    "(type IN ('ENTRADA', 'SALIDA') AND quantity > 0) "
    "OR (type = 'ADJUSTMENT' AND quantity <> 0)"
)
ADJUSTMENT_REASON_CHECK = "(type <> 'ADJUSTMENT') OR (reason IS NOT NULL AND reason <> '')"

BACKFILL_SQL = sa.text(
    """
    UPDATE inventory_movements m
    SET location_id = v.location_id
    FROM service_consumptions sc
    JOIN service_executions se
      ON se.organization_id = sc.organization_id AND se.id = sc.service_execution_id
    JOIN visits v
      ON v.organization_id = se.organization_id AND v.id = se.visit_id
    WHERE m.id_consumo_origen IS NOT NULL
      AND sc.organization_id = m.organization_id
      AND sc.id = m.id_consumo_origen
    """
)

TRANSFER_PAIR_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION trg_inventory_movements_transfer_pair()
RETURNS trigger AS $$
DECLARE
    pair RECORD;
BEGIN
    IF NEW.type IN ('TRANSFER_OUT', 'TRANSFER_IN') THEN
        IF NEW.transfer_id IS NULL THEN
            RAISE EXCEPTION 'TRANSFER movement requires a transfer_id';
        END IF;
        IF NEW.type = 'TRANSFER_OUT' THEN
            SELECT * INTO pair FROM inventory_movements
            WHERE transfer_id = NEW.transfer_id AND type = 'TRANSFER_IN';
            IF pair.id IS NULL THEN
                RAISE EXCEPTION 'TRANSFER_OUT requires a paired TRANSFER_IN';
            END IF;
        ELSE
            SELECT * INTO pair FROM inventory_movements
            WHERE transfer_id = NEW.transfer_id AND type = 'TRANSFER_OUT';
            IF pair.id IS NULL THEN
                RAISE EXCEPTION 'TRANSFER_IN requires a paired TRANSFER_OUT';
            END IF;
        END IF;
        IF pair.organization_id <> NEW.organization_id
           OR pair.product_id <> NEW.product_id
           OR pair.quantity <> NEW.quantity THEN
            RAISE EXCEPTION 'TRANSFER pair must share organization, product and quantity';
        END IF;
        IF pair.location_id = NEW.location_id THEN
            RAISE EXCEPTION 'TRANSFER pair must move between distinct locations';
        END IF;
    ELSIF NEW.transfer_id IS NOT NULL THEN
        RAISE EXCEPTION 'transfer_id is only valid on TRANSFER movements';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _backfill_consumption_linked_locations() -> None:
    conn = op.get_bind()
    conn.execute(BACKFILL_SQL)
    remaining = conn.execute(
        sa.text(
            "SELECT count(*) FROM inventory_movements WHERE location_id IS NULL"
        )
    ).scalar()
    if remaining:
        raise RuntimeError(
            "inventory_movements has "
            f"{remaining} row(s) with no consumption origin (org-level stock). "
            "There is no truthful location for them; the upgrade refuses to "
            "fabricate one. Resolve each row explicitly (remove it, or stage "
            "it with an operator-approved location) and re-run the migration."
        )


def upgrade() -> None:
    op.add_column(
        "inventory_movements",
        sa.Column("location_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_inventory_movements_organization_location",
        "inventory_movements",
        "locations",
        ["organization_id", "location_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )

    _backfill_consumption_linked_locations()

    op.alter_column("inventory_movements", "location_id", nullable=False)

    op.drop_constraint("ck_inventory_movements_type", "inventory_movements")
    op.drop_constraint("ck_inventory_movements_quantity", "inventory_movements")
    op.create_check_constraint(
        "ck_inventory_movements_type", "inventory_movements", MOVEMENT_TYPE_CHECK
    )
    op.create_check_constraint(
        "ck_inventory_movements_quantity", "inventory_movements", QUANTITY_CHECK
    )

    op.add_column(
        "inventory_movements",
        sa.Column("transfer_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "uq_inventory_movements_transfer_out",
        "inventory_movements",
        ["transfer_id"],
        unique=True,
        postgresql_where=sa.text("type = 'TRANSFER_OUT'"),
    )
    op.create_index(
        "uq_inventory_movements_transfer_in",
        "inventory_movements",
        ["transfer_id"],
        unique=True,
        postgresql_where=sa.text("type = 'TRANSFER_IN'"),
    )
    op.create_index(
        "ix_inventory_movements_org_product_location",
        "inventory_movements",
        ["organization_id", "product_id", "location_id", "id"],
    )

    op.execute(sa.text(TRANSFER_PAIR_TRIGGER_FUNCTION))
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER trg_inventory_movements_transfer_pair
            AFTER INSERT OR UPDATE OF transfer_id, type ON inventory_movements
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION trg_inventory_movements_transfer_pair();
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_inventory_movements_transfer_pair"
            " ON inventory_movements"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS trg_inventory_movements_transfer_pair()"))

    # Transfer rows cannot exist at 0007 (the old type CHECK forbids them);
    # they are dropped with their migration, never with the data.
    op.execute(
        sa.text(
            "DELETE FROM inventory_movements"
            " WHERE type IN ('TRANSFER_OUT', 'TRANSFER_IN')"
        )
    )
    op.drop_index(
        "uq_inventory_movements_transfer_out", table_name="inventory_movements"
    )
    op.drop_index(
        "uq_inventory_movements_transfer_in", table_name="inventory_movements"
    )
    op.drop_index(
        "ix_inventory_movements_org_product_location",
        table_name="inventory_movements",
    )
    op.drop_column("inventory_movements", "transfer_id")

    op.drop_constraint("ck_inventory_movements_type", "inventory_movements")
    op.drop_constraint("ck_inventory_movements_quantity", "inventory_movements")
    op.create_check_constraint(
        "ck_inventory_movements_type", "inventory_movements", V1_TYPE_CHECK
    )
    op.create_check_constraint(
        "ck_inventory_movements_quantity", "inventory_movements", V1_QUANTITY_CHECK
    )

    op.drop_constraint(
        "fk_inventory_movements_organization_location", "inventory_movements"
    )
    op.drop_column("inventory_movements", "location_id")
