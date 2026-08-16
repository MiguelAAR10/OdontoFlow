"""Inventory application services: entries, adjustments, kardex, balance.

The ledger is the single source of stock truth; the balance is derived at
read time (``Σ ENTRADA − Σ SALIDA + Σ signed ADJUSTMENT``) — no stored
``stock_actual``, no trigger cache (contract: BALANCE_DERIVATION_STRATEGY).
Every mutation follows the module conventions: claim-first PF4, ctx-gated
permissions (PF2), atomic audit (PF3), one ``session.begin()`` per command.
The negative-balance guard serializes per ``(organization_id, product_id)``
by locking the product row ``FOR UPDATE`` before summing the ledger.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.context import default_context
from app.economics.models import Product
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import MOVEMENTS_CREATE, MOVEMENTS_READ
from app.iam.service import require_permission
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.inventory.models import ADJUSTMENT, ENTRADA, InventoryMovement
from app.inventory.schemas import AdjustmentCreate, EntryCreate
from app.tenancy import scoped

OP_ENTRIES_CREATE = "inventory.entries.create"
OP_ADJUSTMENTS_CREATE = "inventory.adjustments.create"

MOVEMENT_ENTITY_TYPE = "inventory_movement"
ENTRY_CREATED_ACTION = "inventory_entry.created"
ADJUSTMENT_CREATED_ACTION = "inventory_adjustment.created"


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    return ctx if ctx is not None else default_context(organization_id)


def _load_product(session: Session, product_id: int, organization_id: int) -> Product:
    product = session.scalar(
        scoped(select(Product).where(Product.id == product_id), Product, organization_id)
    )
    if product is None:
        raise AppError(ErrorCode.NOT_FOUND, "Product not found.")
    return product


def available_balance(
    session: Session, product_id: int, organization_id: int
) -> Decimal:
    """The derived available quantity from the ledger (read-time aggregate)."""
    rows = session.execute(
        select(InventoryMovement.type, InventoryMovement.quantity).where(
            InventoryMovement.organization_id == organization_id,
            InventoryMovement.product_id == product_id,
        )
    ).all()
    available = Decimal("0")
    for movement_type, quantity in rows:
        if movement_type == ENTRADA:
            available += quantity
        elif movement_type == ADJUSTMENT:
            available += quantity  # signed
        else:  # SALIDA
            available -= quantity
    return available


def require_stock(
    session: Session, product_id: int, organization_id: int, required: Decimal
) -> None:
    """Serialize per (org, product) and reject an insufficient balance.

    The product row lock makes concurrent SALIDA/adjustment commands queue on
    the same product, so the ledger SUM below is authoritative; the DB CHECKs
    back the per-type quantity rules. This is the single stock-floor guard
    every stock-out path (consumption, sale, negative adjustment) goes
    through.
    """
    session.execute(
        scoped(select(Product).where(Product.id == product_id), Product, organization_id)
        .with_for_update()
    )
    if available_balance(session, product_id, organization_id) < required:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Stock insuficiente para el movimiento solicitado.",
        )


def register_entry(
    session: Session,
    product_id: int,
    data: EntryCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> InventoryMovement:
    """Record a purchase/initial input (ENTRADA) on the ledger."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, MOVEMENTS_CREATE)

        _load_product(session, product_id, org_id)
        movement = InventoryMovement(
            organization_id=org_id,
            product_id=product_id,
            type=ENTRADA,
            quantity=data.quantity,
            unit_price=data.unit_price,
        )
        session.add(movement)
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=MOVEMENT_ENTITY_TYPE,
            entity_id=str(movement.id),
            action=ENTRY_CREATED_ACTION,
            after_state={
                "id": movement.id,
                "product_id": movement.product_id,
                "quantity": str(movement.quantity),
            },
        )
        settle_receipt(
            receipt,
            resource_type=MOVEMENT_ENTITY_TYPE,
            resource_id=str(movement.id),
            outcome_json={
                "status": "applied",
                "resource_type": MOVEMENT_ENTITY_TYPE,
                "resource_id": str(movement.id),
                "product_id": movement.product_id,
                "type": movement.type,
                "quantity": str(movement.quantity),
                "unit_price": str(movement.unit_price) if movement.unit_price is not None else None,
                "moved_at": movement.moved_at.isoformat(),
            },
        )

    return movement


def register_adjustment(
    session: Session,
    product_id: int,
    data: AdjustmentCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> InventoryMovement:
    """Record a reason-required correction.

    A negative adjustment is a stock-out like any other: the balance guard
    applies (the legacy ``ajustar_stock`` silent-write hole is closed).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, MOVEMENTS_CREATE)

        _load_product(session, product_id, org_id)
        if data.quantity < 0:
            require_stock(session, product_id, org_id, -data.quantity)

        movement = InventoryMovement(
            organization_id=org_id,
            product_id=product_id,
            type=ADJUSTMENT,
            quantity=data.quantity,
            reason=data.reason,
        )
        session.add(movement)
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=MOVEMENT_ENTITY_TYPE,
            entity_id=str(movement.id),
            action=ADJUSTMENT_CREATED_ACTION,
            after_state={
                "id": movement.id,
                "product_id": movement.product_id,
                "quantity": str(movement.quantity),
                "reason": movement.reason,
            },
        )
        settle_receipt(
            receipt,
            resource_type=MOVEMENT_ENTITY_TYPE,
            resource_id=str(movement.id),
            outcome_json={
                "status": "applied",
                "resource_type": MOVEMENT_ENTITY_TYPE,
                "resource_id": str(movement.id),
                "product_id": movement.product_id,
                "type": movement.type,
                "quantity": str(movement.quantity),
                "reason": movement.reason,
                "moved_at": movement.moved_at.isoformat(),
            },
        )

    return movement


def list_movements(
    session: Session,
    product_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> list[InventoryMovement]:
    """The kardex: an ordered ledger query, never a cached column."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, MOVEMENTS_READ)
    _load_product(session, product_id, org_id)
    return list(
        session.scalars(
            select(InventoryMovement)
            .where(
                InventoryMovement.organization_id == org_id,
                InventoryMovement.product_id == product_id,
            )
            .order_by(InventoryMovement.id)
        )
    )


def get_balance(
    session: Session,
    product_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> Decimal:
    """The derived available quantity of one product (read-time)."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, MOVEMENTS_READ)
    _load_product(session, product_id, org_id)
    return available_balance(session, product_id, org_id)
