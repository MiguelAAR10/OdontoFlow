"""Inventory application services: entries, adjustments, transfers, kardex, balance.

The ledger is the single source of stock truth; the balance is derived at
read time (``Σ ENTRADA + Σ TRANSFER_IN − Σ SALIDA − Σ TRANSFER_OUT + Σ signed
ADJUSTMENT``) per ``(organization_id, product_id, location_id)`` — no stored
``stock_actual``, no trigger cache (contract: BALANCE_DERIVATION_STRATEGY,
M4.2). Every mutation follows the module conventions: claim-first PF4,
ctx-gated permissions (PF2), atomic audit (PF3), one ``session.begin()`` per
command. The negative-balance guard serializes per ``(organization_id,
product_id)`` by locking the product row ``FOR UPDATE`` before summing the
ledger of the target location.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.context import default_context
from app.economics.models import Product
from app.errors import AppError, ErrorCode
from app.events.service import record_domain_event
from app.events.types import INVENTORY_BELOW_REORDER
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    MOVEMENTS_CREATE,
    MOVEMENTS_READ,
    PRODUCTS_READ,
    REORDER_POINTS_MANAGE,
)
from app.iam.service import require_permission
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.inventory.models import (
    ADJUSTMENT,
    ENTRADA,
    TRANSFER_IN,
    TRANSFER_OUT,
    InventoryMovement,
    ReorderPoint,
)
from app.inventory.schemas import (
    AdjustmentCreate,
    EntryCreate,
    ReorderPointUpsert,
    TransferCreate,
)
from app.organization.models import Location
from app.tenancy import scoped

OP_ENTRIES_CREATE = "inventory.entries.create"
OP_ADJUSTMENTS_CREATE = "inventory.adjustments.create"
OP_TRANSFERS_CREATE = "inventory.transfers.create"

MOVEMENT_ENTITY_TYPE = "inventory_movement"
ENTRY_CREATED_ACTION = "inventory_entry.created"
ADJUSTMENT_CREATED_ACTION = "inventory_adjustment.created"
TRANSFER_ENTITY_TYPE = "inventory_transfer"
TRANSFER_CREATED_ACTION = "inventory_transfer.created"
OP_REORDER_POINTS_SET = "inventory.reorder_points.set"
REORDER_POINT_ENTITY_TYPE = "reorder_point"
REORDER_POINT_SET_ACTION = "reorder_point.set"


@dataclass(frozen=True, slots=True)
class TransferResult:
    """The logical outcome of one transfer command (two ledger rows)."""

    transfer_id: str
    product_id: int
    origin_location_id: int
    destination_location_id: int
    quantity: Decimal
    reason: str | None
    out_movement_id: int
    in_movement_id: int


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


def _load_location(session: Session, location_id: int, organization_id: int) -> Location:
    location = session.scalar(
        scoped(select(Location).where(Location.id == location_id), Location, organization_id)
    )
    if location is None:
        raise AppError(ErrorCode.NOT_FOUND, "Location not found.")
    return location


def available_balance(
    session: Session,
    product_id: int,
    organization_id: int,
    location_id: int,
) -> Decimal:
    """The derived available quantity of one product at one location (read-time)."""
    rows = session.execute(
        select(InventoryMovement.type, InventoryMovement.quantity).where(
            InventoryMovement.organization_id == organization_id,
            InventoryMovement.product_id == product_id,
            InventoryMovement.location_id == location_id,
        )
    ).all()
    available = Decimal("0")
    for movement_type, quantity in rows:
        if movement_type in (ENTRADA, TRANSFER_IN):
            available += quantity
        elif movement_type == ADJUSTMENT:
            available += quantity  # signed
        else:  # SALIDA / TRANSFER_OUT
            available -= quantity
    return available


def require_stock(
    session: Session,
    product_id: int,
    organization_id: int,
    required: Decimal,
    location_id: int,
) -> None:
    """Serialize per (org, product) and reject an insufficient balance.

    The product row lock makes concurrent SALIDA/adjustment/transfer commands
    queue on the same product, so the ledger SUM below is authoritative; the DB
    CHECKs back the per-type quantity rules. This is the single stock-floor
    guard every stock-out path (consumption, transfer, negative adjustment)
    goes through — evaluated against the location the stock leaves.
    """
    session.execute(
        scoped(select(Product).where(Product.id == product_id), Product, organization_id)
        .with_for_update()
    )
    if available_balance(session, product_id, organization_id, location_id) < required:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Stock insuficiente para el movimiento solicitado.",
        )


def emit_below_reorder_if_crossed(
    session: Session,
    ctx: ExecutionContext,
    *,
    product_id: int,
    location_id: int,
    balance_before: Decimal,
    balance_after: Decimal,
    movement_id: int,
) -> None:
    """B0.5: stage ``inventory.below_reorder`` only on the crossing.

    Called by every stock-out path (negative adjustment, transfer origin,
    consumption SALIDA) while the product row lock is held, with the balances
    around the movement. A movement that starts already below the minimum does
    not emit again; without a reorder point nothing is emitted.
    """
    minimum = session.scalar(
        select(ReorderPoint.min_quantity).where(
            ReorderPoint.organization_id == ctx.organization_id,
            ReorderPoint.product_id == product_id,
            ReorderPoint.location_id == location_id,
        )
    )
    if minimum is None or not (balance_before >= minimum > balance_after):
        return
    record_domain_event(
        session,
        ctx=ctx,
        event_type=INVENTORY_BELOW_REORDER,
        aggregate_type=REORDER_POINT_ENTITY_TYPE,
        aggregate_id=f"{product_id}:{location_id}",
        payload={
            "product_id": product_id,
            "location_id": location_id,
            "min_quantity": str(minimum),
            "balance_before": str(balance_before),
            "balance_after": str(balance_after),
            "movement_id": movement_id,
        },
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
    """Record a purchase/initial input (ENTRADA) on the ledger, at one location."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, MOVEMENTS_CREATE)

        _load_product(session, product_id, org_id)
        _load_location(session, data.location_id, org_id)
        movement = InventoryMovement(
            organization_id=org_id,
            product_id=product_id,
            location_id=data.location_id,
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
                "location_id": movement.location_id,
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
                "location_id": movement.location_id,
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
    """Record a reason-required correction at one location.

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
        _load_location(session, data.location_id, org_id)
        balance_before = None
        if data.quantity < 0:
            require_stock(session, product_id, org_id, -data.quantity, data.location_id)
            balance_before = available_balance(session, product_id, org_id, data.location_id)

        movement = InventoryMovement(
            organization_id=org_id,
            product_id=product_id,
            location_id=data.location_id,
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
                "location_id": movement.location_id,
                "quantity": str(movement.quantity),
                "reason": movement.reason,
            },
        )
        if balance_before is not None:
            emit_below_reorder_if_crossed(
                session,
                resolved,
                product_id=product_id,
                location_id=data.location_id,
                balance_before=balance_before,
                balance_after=balance_before + data.quantity,
                movement_id=movement.id,
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
                "location_id": movement.location_id,
                "type": movement.type,
                "quantity": str(movement.quantity),
                "reason": movement.reason,
                "moved_at": movement.moved_at.isoformat(),
            },
        )

    return movement


def transfer_product(
    session: Session,
    product_id: int,
    data: TransferCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> TransferResult:
    """Move stock between two locations of the same organization, atomically.

    One ``session.begin()``: TRANSFER_OUT at the origin and TRANSFER_IN at the
    destination share ``transfer_id`` (server-generated UUID); the DB partial
    uniques and the deferred pair trigger make a partial or inconsistent pair
    structurally impossible at COMMIT. Stock-conserving by construction
    (OUT and IN carry the same positive quantity); the origin floor is the
    ledger sum of the origin location under the product row lock, so
    concurrent transfers/consumptions can never overdraw it.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, MOVEMENTS_CREATE)

        _load_product(session, product_id, org_id)
        _load_location(session, data.origin_location_id, org_id)
        _load_location(session, data.destination_location_id, org_id)
        if data.origin_location_id == data.destination_location_id:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The transfer origin and destination must be different locations.",
            )

        require_stock(session, product_id, org_id, data.quantity, data.origin_location_id)
        origin_before = available_balance(session, product_id, org_id, data.origin_location_id)

        transfer_id = str(uuid.uuid4())
        out_movement = InventoryMovement(
            organization_id=org_id,
            product_id=product_id,
            location_id=data.origin_location_id,
            type=TRANSFER_OUT,
            quantity=data.quantity,
            reason=data.reason,
            transfer_id=transfer_id,
        )
        in_movement = InventoryMovement(
            organization_id=org_id,
            product_id=product_id,
            location_id=data.destination_location_id,
            type=TRANSFER_IN,
            quantity=data.quantity,
            reason=data.reason,
            transfer_id=transfer_id,
        )
        session.add_all([out_movement, in_movement])
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=TRANSFER_ENTITY_TYPE,
            entity_id=transfer_id,
            action=TRANSFER_CREATED_ACTION,
            after_state={
                "transfer_id": transfer_id,
                "product_id": product_id,
                "origin_location_id": data.origin_location_id,
                "destination_location_id": data.destination_location_id,
                "quantity": str(data.quantity),
                "out_movement_id": out_movement.id,
                "in_movement_id": in_movement.id,
            },
        )
        emit_below_reorder_if_crossed(
            session,
            resolved,
            product_id=product_id,
            location_id=data.origin_location_id,
            balance_before=origin_before,
            balance_after=origin_before - data.quantity,
            movement_id=out_movement.id,
        )
        result = TransferResult(
            transfer_id=transfer_id,
            product_id=product_id,
            origin_location_id=data.origin_location_id,
            destination_location_id=data.destination_location_id,
            quantity=data.quantity,
            reason=data.reason,
            out_movement_id=out_movement.id,
            in_movement_id=in_movement.id,
        )
        settle_receipt(
            receipt,
            resource_type=TRANSFER_ENTITY_TYPE,
            resource_id=transfer_id,
            outcome_json={
                "status": "applied",
                "resource_type": TRANSFER_ENTITY_TYPE,
                "resource_id": transfer_id,
                "transfer_id": transfer_id,
                "product_id": product_id,
                "origin_location_id": data.origin_location_id,
                "destination_location_id": data.destination_location_id,
                "quantity": str(data.quantity),
                "reason": data.reason,
                "out_movement_id": out_movement.id,
                "in_movement_id": in_movement.id,
            },
        )

    return result


def list_movements(
    session: Session,
    product_id: int,
    *,
    location_id: int,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> list[InventoryMovement]:
    """The kardex of one product at one location: an ordered ledger query."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, MOVEMENTS_READ)
    _load_product(session, product_id, org_id)
    _load_location(session, location_id, org_id)
    return list(
        session.scalars(
            select(InventoryMovement)
            .where(
                InventoryMovement.organization_id == org_id,
                InventoryMovement.product_id == product_id,
                InventoryMovement.location_id == location_id,
            )
            .order_by(InventoryMovement.id)
        )
    )


def get_balance(
    session: Session,
    product_id: int,
    *,
    location_id: int,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> Decimal:
    """The derived available quantity of one product at one location (read-time)."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, MOVEMENTS_READ)
    _load_product(session, product_id, org_id)
    _load_location(session, location_id, org_id)
    return available_balance(session, product_id, org_id, location_id)


# --- B0.5: reorder points and low stock -------------------------------------


@dataclass(frozen=True, slots=True)
class LowStockRow:
    product_id: int
    product_name: str
    unit: str
    location_id: int
    location_name: str
    balance: Decimal
    min_quantity: Decimal


def _reorder_point_state(point: ReorderPoint | None) -> dict | None:
    if point is None:
        return None
    return {
        "id": point.id,
        "product_id": point.product_id,
        "location_id": point.location_id,
        "min_quantity": str(point.min_quantity),
    }


def upsert_reorder_point(
    session: Session,
    product_id: int,
    location_id: int,
    data: ReorderPointUpsert,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> ReorderPoint:
    """Set the minimum of one product at one location (PUT semantics).

    ``INSERT … ON CONFLICT (organization_id, product_id, location_id) DO
    UPDATE``: repeating the call leaves one row. Setting a minimum is not a
    stock movement, so it never emits ``inventory.below_reorder``.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, REORDER_POINTS_MANAGE)
        product = _load_product(session, product_id, org_id)
        location = _load_location(session, location_id, org_id)
        if not product.is_active or not location.is_active:
            raise AppError(ErrorCode.ENTITY_INACTIVE, "Product or location is inactive.")

        before = session.scalar(
            select(ReorderPoint).where(
                ReorderPoint.organization_id == org_id,
                ReorderPoint.product_id == product_id,
                ReorderPoint.location_id == location_id,
            )
        )
        before_state = _reorder_point_state(before)
        statement = (
            insert(ReorderPoint)
            .values(
                organization_id=org_id,
                product_id=product_id,
                location_id=location_id,
                min_quantity=data.min_quantity,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "product_id", "location_id"],
                set_={"min_quantity": data.min_quantity, "updated_at": func.now()},
            )
            .returning(ReorderPoint.id)
        )
        point_id = session.execute(statement).scalar_one()
        point = session.get(ReorderPoint, point_id, populate_existing=True)

        record_event(
            session,
            ctx=resolved,
            entity_type=REORDER_POINT_ENTITY_TYPE,
            entity_id=str(point.id),
            action=REORDER_POINT_SET_ACTION,
            before_state=before_state,
            after_state=_reorder_point_state(point),
        )
        settle_receipt(
            receipt,
            resource_type=REORDER_POINT_ENTITY_TYPE,
            resource_id=str(point.id),
            outcome_json={
                "status": "applied",
                "resource_type": REORDER_POINT_ENTITY_TYPE,
                "resource_id": str(point.id),
                "product_id": point.product_id,
                "location_id": point.location_id,
                "min_quantity": str(point.min_quantity),
                "updated_at": point.updated_at.isoformat(),
            },
        )

    return point


def list_low_stock(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    location_id: int | None = None,
) -> list[LowStockRow]:
    """Products whose derived balance is strictly below their reorder point.

    One query: the signed ledger sum per reorder point (same derivation as
    :func:`available_balance`), compared against ``min_quantity``.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PRODUCTS_READ)
        require_permission(session, resolved, MOVEMENTS_READ)
    if location_id is not None:
        _load_location(session, location_id, org_id)

    signed = case(
        (InventoryMovement.type.in_((ENTRADA, TRANSFER_IN, ADJUSTMENT)), InventoryMovement.quantity),
        else_=-InventoryMovement.quantity,
    )
    balance = (
        select(func.coalesce(func.sum(signed), 0))
        .where(
            InventoryMovement.organization_id == ReorderPoint.organization_id,
            InventoryMovement.product_id == ReorderPoint.product_id,
            InventoryMovement.location_id == ReorderPoint.location_id,
        )
        .correlate(ReorderPoint)
        .scalar_subquery()
    )
    statement = (
        select(ReorderPoint, Product.name, Product.unit, Location.name, balance)
        .join(
            Product,
            (Product.organization_id == ReorderPoint.organization_id)
            & (Product.id == ReorderPoint.product_id),
        )
        .join(
            Location,
            (Location.organization_id == ReorderPoint.organization_id)
            & (Location.id == ReorderPoint.location_id),
        )
        .where(ReorderPoint.organization_id == org_id, balance < ReorderPoint.min_quantity)
        .order_by(ReorderPoint.location_id, ReorderPoint.product_id)
    )
    if location_id is not None:
        statement = statement.where(ReorderPoint.location_id == location_id)
    return [
        LowStockRow(
            product_id=point.product_id,
            product_name=product_name,
            unit=unit,
            location_id=point.location_id,
            location_name=location_name,
            balance=Decimal(value),
            min_quantity=point.min_quantity,
        )
        for point, product_name, unit, location_name, value in session.execute(statement).all()
    ]
