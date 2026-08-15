"""Economic & operations HTTP surface: products, consumptions, charges, payments.

Thin transport: HTTP shape → schema → application service → typed response.
The `Idempotency-Key` header is passed straight through to the PF4 command
handler for every create (C10). Monetary reads expose derived amounts only
(paid/outstanding are never stored).
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.economics.schemas import (
    ChargeCreate,
    ChargeRead,
    PaymentCreate,
    PaymentRead,
    ProductCreate,
    ProductRead,
    ServiceConsumptionCreate,
    ServiceConsumptionRead,
)
from app.economics.service import (
    OP_CHARGES_CREATE,
    OP_CONSUMPTIONS_CREATE,
    OP_PAYMENTS_CREATE,
    OP_PRODUCTS_CREATE,
    charge_paid_amount,
    create_charge,
    create_payment,
    create_product,
    create_service_consumption,
    get_charge,
    get_product,
    list_charges,
    list_consumptions,
    list_payments,
    list_products,
)
from app.idempotency.service import run_idempotent_command

router = APIRouter()

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get(IDEMPOTENCY_HEADER)
    if value is None or value == "":
        return None
    return value


def _product_read(product) -> ProductRead:
    return ProductRead(
        id=product.id,
        name=product.name,
        unit=product.unit,
        kind=product.kind,
        is_active=product.is_active,
    )


def _product_read_from_outcome(outcome: dict) -> ProductRead:
    return ProductRead(
        id=int(outcome["resource_id"]),
        name=outcome["name"],
        unit=outcome["unit"],
        kind=outcome["kind"],
        is_active=True,
    )


def _consumption_read(consumption) -> ServiceConsumptionRead:
    return ServiceConsumptionRead(
        id=consumption.id,
        service_execution_id=consumption.service_execution_id,
        product_id=consumption.product_id,
        product_name=consumption.product.name,
        quantity=consumption.quantity,
        unit_price=consumption.unit_price,
        amount=(consumption.quantity * consumption.unit_price).quantize(Decimal("0.01")),
        consumed_at=consumption.consumed_at,
    )


def _consumption_read_from_outcome(outcome: dict) -> ServiceConsumptionRead:
    from datetime import datetime as _dt

    quantity = Decimal(outcome["quantity"])
    unit_price = Decimal(outcome["unit_price"])
    return ServiceConsumptionRead(
        id=int(outcome["resource_id"]),
        service_execution_id=outcome["service_execution_id"],
        product_id=outcome["product_id"],
        product_name=outcome["product_name"],
        quantity=quantity,
        unit_price=unit_price,
        amount=(quantity * unit_price).quantize(Decimal("0.01")),
        consumed_at=_dt.fromisoformat(outcome["consumed_at"]),
    )


def _charge_read(charge, paid: Decimal) -> ChargeRead:
    return ChargeRead(
        id=charge.id,
        service_execution_id=charge.service_execution_id,
        amount=charge.amount,
        paid=paid,
        outstanding=charge.amount - paid,
        created_at=charge.created_at,
    )


def _charge_read_from_outcome(outcome: dict) -> ChargeRead:
    from datetime import datetime as _dt

    amount = Decimal(outcome["amount"])
    return ChargeRead(
        id=int(outcome["resource_id"]),
        service_execution_id=outcome["service_execution_id"],
        amount=amount,
        paid=Decimal("0"),
        outstanding=amount,
        created_at=_dt.fromisoformat(outcome["created_at"]),
    )


def _payment_read(payment) -> PaymentRead:
    return PaymentRead(
        id=payment.id,
        charge_id=payment.charge_id,
        amount=payment.amount,
        method=payment.method,
        paid_at=payment.paid_at,
    )


def _payment_read_from_outcome(outcome: dict) -> PaymentRead:
    from datetime import datetime as _dt

    return PaymentRead(
        id=int(outcome["resource_id"]),
        charge_id=outcome["charge_id"],
        amount=Decimal(outcome["amount"]),
        method=outcome["method"],
        paid_at=_dt.fromisoformat(outcome["paid_at"]),
    )


@router.post("/products", response_model=ProductRead, status_code=201)
def create_product_route(
    payload: ProductCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ProductRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_product,
        operation_name=OP_PRODUCTS_CREATE,
        key=key,
        ctx=ctx,
        params=payload.model_dump(),
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _product_read_from_outcome(outcome.outcome)
    return _product_read(outcome.result)


@router.get("/products", response_model=list[ProductRead])
def list_products_route(
    request: Request,
    db: Session = Depends(get_db),
    search: str | None = None,
    kind: str | None = None,
) -> list[ProductRead]:
    ctx = resolve_http_context(request)
    return [_product_read(p) for p in list_products(db, ctx=ctx, search=search, kind=kind)]


@router.get("/products/{product_id}", response_model=ProductRead)
def get_product_route(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> ProductRead:
    ctx = resolve_http_context(request)
    return _product_read(get_product(db, product_id, ctx=ctx))


@router.post("/executions/{execution_id}/consumptions", response_model=ServiceConsumptionRead, status_code=201)
def create_service_consumption_route(
    execution_id: int,
    payload: ServiceConsumptionCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ServiceConsumptionRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_service_consumption,
        operation_name=OP_CONSUMPTIONS_CREATE,
        key=key,
        ctx=ctx,
        params={"service_execution_id": execution_id, **payload.model_dump()},
        execution_id=execution_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _consumption_read_from_outcome(outcome.outcome)
    return _consumption_read(outcome.result)


@router.get("/executions/{execution_id}/consumptions", response_model=list[ServiceConsumptionRead])
def list_service_consumptions_route(
    execution_id: int, request: Request, db: Session = Depends(get_db)
) -> list[ServiceConsumptionRead]:
    ctx = resolve_http_context(request)
    return [_consumption_read(c) for c in list_consumptions(db, execution_id, ctx=ctx)]


@router.post("/executions/{execution_id}/charges", response_model=ChargeRead, status_code=201)
def create_charge_route(
    execution_id: int,
    payload: ChargeCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ChargeRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_charge,
        operation_name=OP_CHARGES_CREATE,
        key=key,
        ctx=ctx,
        params={"service_execution_id": execution_id, **payload.model_dump()},
        execution_id=execution_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _charge_read_from_outcome(outcome.outcome)
    return _charge_read(outcome.result, paid=Decimal("0"))


@router.get("/charges", response_model=list[ChargeRead])
def list_charges_route(
    request: Request,
    db: Session = Depends(get_db),
    execution_id: int | None = None,
) -> list[ChargeRead]:
    ctx = resolve_http_context(request)
    charges = list_charges(db, ctx=ctx, execution_id=execution_id)
    return [_charge_read(c, paid=charge_paid_amount(db, c.id, ctx.organization_id)) for c in charges]


@router.get("/charges/{charge_id}", response_model=ChargeRead)
def get_charge_route(
    charge_id: int, request: Request, db: Session = Depends(get_db)
) -> ChargeRead:
    ctx = resolve_http_context(request)
    charge = get_charge(db, charge_id, ctx=ctx)
    return _charge_read(charge, paid=charge_paid_amount(db, charge.id, ctx.organization_id))


@router.post("/charges/{charge_id}/payments", response_model=PaymentRead, status_code=201)
def create_payment_route(
    charge_id: int,
    payload: PaymentCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> PaymentRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=create_payment,
        operation_name=OP_PAYMENTS_CREATE,
        key=key,
        ctx=ctx,
        params={"charge_id": charge_id, **payload.model_dump()},
        charge_id=charge_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _payment_read_from_outcome(outcome.outcome)
    return _payment_read(outcome.result)


@router.get("/charges/{charge_id}/payments", response_model=list[PaymentRead])
def list_payments_route(
    charge_id: int, request: Request, db: Session = Depends(get_db)
) -> list[PaymentRead]:
    ctx = resolve_http_context(request)
    return [_payment_read(p) for p in list_payments(db, charge_id, ctx=ctx)]
