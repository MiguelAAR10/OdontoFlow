"""Inventory HTTP surface: entries, adjustments, kardex, balance.

Thin transport; every create is PF4-idempotent; balance is derived at read.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.idempotency.service import run_idempotent_command
from app.inventory.schemas import (
    AdjustmentCreate,
    BalanceRead,
    EntryCreate,
    MovementRead,
)
from app.inventory.service import (
    OP_ADJUSTMENTS_CREATE,
    OP_ENTRIES_CREATE,
    get_balance,
    list_movements,
    register_adjustment,
    register_entry,
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
        type=movement.type,
        quantity=movement.quantity,
        unit_price=movement.unit_price,
        reason=movement.reason,
        id_consumo_origen=movement.id_consumo_origen,
        moved_at=movement.moved_at,
    )


def _movement_read_from_outcome(outcome: dict) -> MovementRead:
    from datetime import datetime as _dt

    return MovementRead(
        id=int(outcome["resource_id"]),
        product_id=outcome["product_id"],
        type=outcome["type"],
        quantity=Decimal(outcome["quantity"]),
        unit_price=Decimal(outcome["unit_price"]) if outcome.get("unit_price") else None,
        reason=outcome.get("reason"),
        id_consumo_origen=outcome.get("id_consumo_origen"),
        moved_at=_dt.fromisoformat(outcome["moved_at"]),
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


@router.get("/products/{product_id}/movements", response_model=list[MovementRead])
def list_movements_route(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> list[MovementRead]:
    ctx = resolve_http_context(request)
    return [_movement_read(m) for m in list_movements(db, product_id, ctx=ctx)]


@router.get("/products/{product_id}/balance", response_model=BalanceRead)
def get_balance_route(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> BalanceRead:
    ctx = resolve_http_context(request)
    return BalanceRead(
        product_id=product_id,
        available=get_balance(db, product_id, ctx=ctx),
    )
