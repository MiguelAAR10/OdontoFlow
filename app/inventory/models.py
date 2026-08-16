"""Inventory ledger domain (PF7): append-only movements, derived balance.

Per ``.audit/economic-ops/next-inventory-contract.md``: ``inventory_movements``
is the ONLY stock truth — no ``stock_actual`` column, no trigger cache, one
authoritative mutation path (the services in this module plus the
consumption→SALIDA command in ``app/economics``). The balance is a derived
read-time aggregate.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

#: The V1 movement vocabulary (contract: MOVEMENT_TYPES_FOR_V1). Uppercase
#: follows the approved contract DDL exactly.
MOVEMENT_TYPE_CHECK = "type IN ('ENTRADA', 'SALIDA', 'ADJUSTMENT')"
QUANTITY_CHECK = (
    "(type IN ('ENTRADA', 'SALIDA') AND quantity > 0) "
    "OR (type = 'ADJUSTMENT' AND quantity <> 0)"
)
ADJUSTMENT_REASON_CHECK = "(type <> 'ADJUSTMENT') OR (reason IS NOT NULL AND reason <> '')"

ENTRADA = "ENTRADA"
SALIDA = "SALIDA"
ADJUSTMENT = "ADJUSTMENT"


class InventoryMovement(Base):
    """One append-only stock event for one organization-owned product.

    Corrections are new rows with a reason, never edits; reversal is an
    offsetting movement, never a delete. ``id_consumo_origen`` keeps the 1:1
    causal link between a consumption and its SALIDA row.
    """

    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_inventory_movements_organization",
        ),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    id_consumo_origen: Mapped[int | None] = mapped_column(nullable=True)
    moved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    product: Mapped["Product"] = relationship(  # noqa: F821
        foreign_keys=[product_id],
        primaryjoin="InventoryMovement.product_id == Product.id",
    )

    __table_args__ = (
        CheckConstraint(MOVEMENT_TYPE_CHECK, name="ck_inventory_movements_type"),
        CheckConstraint(QUANTITY_CHECK, name="ck_inventory_movements_quantity"),
        CheckConstraint(ADJUSTMENT_REASON_CHECK, name="ck_inventory_movements_reason"),
        UniqueConstraint(
            "organization_id", "id", name="uq_inventory_movements_organization_id"
        ),
        UniqueConstraint(
            "id_consumo_origen", name="uq_inventory_movements_consumo_origen"
        ),
        ForeignKeyConstraint(
            ["organization_id", "product_id"],
            ["products.organization_id", "products.id"],
            ondelete="RESTRICT",
            name="fk_inventory_movements_organization_product",
        ),
        ForeignKeyConstraint(
            ["organization_id", "id_consumo_origen"],
            ["service_consumptions.organization_id", "service_consumptions.id"],
            ondelete="RESTRICT",
            name="fk_inventory_movements_organization_consumption",
        ),
        Index(
            "ix_inventory_movements_org_product",
            "organization_id",
            "product_id",
            "id",
        ),
    )
