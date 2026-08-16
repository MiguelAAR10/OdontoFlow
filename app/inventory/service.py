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
from app.inventory.models import (
    ADJUSTMENT,
    ENTRADA,
    TRANSFER_IN,
    TRANSFER_OUT,
    InventoryMovement,
)
from app.inventory.schemas import AdjustmentCreate, EntryCreate, TransferCreate
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
        if data.quantity < 0:
            require_stock(session, product_id, org_id, -data.quantity, data.location_id)

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
