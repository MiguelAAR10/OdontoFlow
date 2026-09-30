"""Inventory HTTP surface: entries, adjustments, transfers, kardex, balance.

Thin transport; every create is PF4-idempotent; balance is derived at read.
Location scoping (M4.2): entries/adjustments carry ``location_id`` in the
body; the kardex and balance take it as a required query parameter; transfers
move stock between two locations of the same organization.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.idempotency.service import run_idempotent_command
from app.inventory.schemas import (
    AdjustmentCreate,
    BalanceRead,
    EntryCreate,
    LowStockRead,
    MovementRead,
    ReorderPointRead,
    ReorderPointUpsert,
    TransferCreate,
    TransferRead,
)
from app.inventory.service import (
    OP_ADJUSTMENTS_CREATE,
    OP_ENTRIES_CREATE,
    OP_REORDER_POINTS_SET,
    OP_TRANSFERS_CREATE,
    get_balance,
    list_low_stock,
    list_movements,
    register_adjustment,
    register_entry,
    transfer_product,
    upsert_reorder_point,
)

router = APIRouter()

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get(IDEMPOTENCY_HEADER)
    if value is None or value == "":
        return None
    return value


def _movement_read(movement) -> MovementRead:
    return MovementRead(
        id=movement.id,
        product_id=movement.product_id,
        location_id=movement.location_id,
        type=movement.type,
        quantity=movement.quantity,
        unit_price=movement.unit_price,
        reason=movement.reason,
        id_consumo_origen=movement.id_consumo_origen,
        transfer_id=movement.transfer_id,
        moved_at=movement.moved_at,
    )


def _movement_read_from_outcome(outcome: dict) -> MovementRead:
    from datetime import datetime as _dt

    return MovementRead(
        id=int(outcome["resource_id"]),
        product_id=outcome["product_id"],
        location_id=outcome["location_id"],
        type=outcome["type"],
        quantity=Decimal(outcome["quantity"]),
        unit_price=Decimal(outcome["unit_price"]) if outcome.get("unit_price") else None,
        reason=outcome.get("reason"),
        id_consumo_origen=outcome.get("id_consumo_origen"),
        transfer_id=outcome.get("transfer_id"),
        moved_at=_dt.fromisoformat(outcome["moved_at"]),
    )


def _transfer_read(result) -> TransferRead:
    return TransferRead(
        transfer_id=result.transfer_id,
        product_id=result.product_id,
        origin_location_id=result.origin_location_id,
        destination_location_id=result.destination_location_id,
        quantity=result.quantity,
        reason=result.reason,
        out_movement_id=result.out_movement_id,
        in_movement_id=result.in_movement_id,
    )


def _transfer_read_from_outcome(outcome: dict) -> TransferRead:
    return TransferRead(
        transfer_id=outcome["transfer_id"],
        product_id=outcome["product_id"],
        origin_location_id=outcome["origin_location_id"],
        destination_location_id=outcome["destination_location_id"],
        quantity=Decimal(outcome["quantity"]),
        reason=outcome.get("reason"),
        out_movement_id=outcome["out_movement_id"],
        in_movement_id=outcome["in_movement_id"],
    )


@router.post("/products/{product_id}/entries", response_model=MovementRead, status_code=201)
def register_entry_route(
    product_id: int,
    payload: EntryCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MovementRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=register_entry,
        operation_name=OP_ENTRIES_CREATE,
        key=key,
        ctx=ctx,
        params={"product_id": product_id, **payload.model_dump()},
        product_id=product_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _movement_read_from_outcome(outcome.outcome)
    return _movement_read(outcome.result)


@router.post("/products/{product_id}/adjustments", response_model=MovementRead, status_code=201)
def register_adjustment_route(
    product_id: int,
    payload: AdjustmentCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> MovementRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=register_adjustment,
        operation_name=OP_ADJUSTMENTS_CREATE,
        key=key,
        ctx=ctx,
        params={"product_id": product_id, **payload.model_dump()},
        product_id=product_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _movement_read_from_outcome(outcome.outcome)
    return _movement_read(outcome.result)


@router.post("/products/{product_id}/transfers", response_model=TransferRead, status_code=201)
def register_transfer_route(
    product_id: int,
    payload: TransferCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TransferRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=transfer_product,
        operation_name=OP_TRANSFERS_CREATE,
        key=key,
        ctx=ctx,
        params={"product_id": product_id, **payload.model_dump()},
        product_id=product_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _transfer_read_from_outcome(outcome.outcome)
    return _transfer_read(outcome.result)


@router.get("/products/{product_id}/movements", response_model=list[MovementRead])
def list_movements_route(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    location_id: int = Query(description="The location whose kardex is read."),
) -> list[MovementRead]:
    ctx = resolve_http_context(request)
    return [_movement_read(m) for m in list_movements(db, product_id, location_id=location_id, ctx=ctx)]


@router.get("/products/{product_id}/balance", response_model=BalanceRead)
def get_balance_route(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    location_id: int = Query(description="The location whose balance is read."),
) -> BalanceRead:
    ctx = resolve_http_context(request)
    return BalanceRead(
        product_id=product_id,
        location_id=location_id,
        available=get_balance(db, product_id, location_id=location_id, ctx=ctx),
    )


# --- B0.5: reorder points and low stock -------------------------------------


@router.put(
    "/products/{product_id}/reorder-points/{location_id}",
    response_model=ReorderPointRead,
    status_code=200,
)
def upsert_reorder_point_route(
    product_id: int,
    location_id: int,
    payload: ReorderPointUpsert,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ReorderPointRead:
    ctx = resolve_http_context(request)
    outcome = run_idempotent_command(
        db,
        operation=upsert_reorder_point,
        operation_name=OP_REORDER_POINTS_SET,
        key=_idempotency_key(request),
        ctx=ctx,
        params={"product_id": product_id, "location_id": location_id, **payload.model_dump()},
        product_id=product_id,
        location_id=location_id,
        data=payload,
    )
    if outcome.replayed:
        from datetime import datetime as _dt

        response.headers[REPLAY_HEADER] = "true"
        stored = outcome.outcome
        return ReorderPointRead(
            id=int(stored["resource_id"]),
            product_id=stored["product_id"],
            location_id=stored["location_id"],
            min_quantity=Decimal(stored["min_quantity"]),
            updated_at=_dt.fromisoformat(stored["updated_at"]),
        )
    return ReorderPointRead.model_validate(outcome.result)


@router.get("/inventory/low-stock", response_model=list[LowStockRead])
def list_low_stock_route(
    request: Request,
    db: Session = Depends(get_db),
    location_id: int | None = Query(default=None, description="Restrict to one location."),
) -> list[LowStockRead]:
    ctx = resolve_http_context(request)
    return [
        LowStockRead(
            product_id=row.product_id,
            product_name=row.product_name,
            unit=row.unit,
            location_id=row.location_id,
            location_name=row.location_name,
            balance=row.balance,
            min_quantity=row.min_quantity,
        )
        for row in list_low_stock(db, ctx=ctx, location_id=location_id)
    ]
