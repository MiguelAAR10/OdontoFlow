"""Economic & operations HTTP surface: products, consumptions, charges, payments.

Thin transport: HTTP shape → schema → application service → typed response.
The `Idempotency-Key` header is passed straight through to the PF4 command
handler for every create (C10). Monetary reads expose derived amounts only
(paid/outstanding are never stored).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.economics.schemas import (
    ChargeCreate,
    ChargeFollowUpClose,
    ChargeFollowUpCreate,
    ChargeFollowUpRead,
    ChargeFollowUpReschedule,
    ChargeRead,
    PaymentCreate,
    PaymentRead,
    PaymentVerify,
    PaymentMethod,
    ProductCreate,
    ProductRead,
    ServiceConsumptionCreate,
    ServiceConsumptionRead,
)
from app.economics.service import (
    OP_CHARGES_CREATE,
    OP_FOLLOW_UPS_CLOSE,
    OP_FOLLOW_UPS_CREATE,
    OP_FOLLOW_UPS_RESCHEDULE,
    OP_CONSUMPTIONS_CREATE,
    OP_PAYMENTS_CREATE,
    OP_PAYMENTS_VERIFY,
    OP_PRODUCTS_CREATE,
    charge_paid_amount,
    create_charge,
    create_payment,
    create_product,
    create_service_consumption,
    get_charge,
    get_product,
    list_charges,
    list_all_payments,
    list_charge_follow_ups,
    list_follow_ups,
    list_consumptions,
    list_payments,
    list_products,
    open_follow_up,
    reschedule_follow_up,
    close_follow_up,
    verify_payment,
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
    paid = getattr(charge, "_fe3a_paid", paid)
    return ChargeRead(
        id=charge.id,
        service_execution_id=charge.service_execution_id,
        amount=charge.amount,
        paid=paid,
        outstanding=charge.amount - paid,
        created_at=charge.created_at,
        visit_id=charge._fe3a_visit_id,
        patient_id=charge._fe3a_patient_id,
        patient_name=charge._fe3a_patient_name,
        service_id=charge._fe3a_service_id,
        service_name=charge._fe3a_service_name,
        location_id=charge._fe3a_location_id,
        location_name=charge._fe3a_location_name,
        practitioner_id=charge._fe3a_practitioner_id,
        practitioner_name=charge._fe3a_practitioner_name,
        executed_at=charge._fe3a_executed_at,
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
        visit_id=outcome["visit_id"],
        patient_id=outcome["patient_id"],
        patient_name=outcome["patient_name"],
        service_id=outcome["service_id"],
        service_name=outcome["service_name"],
        location_id=outcome["location_id"],
        location_name=outcome["location_name"],
        practitioner_id=outcome["practitioner_id"],
        practitioner_name=outcome["practitioner_name"],
        executed_at=_dt.fromisoformat(outcome["executed_at"]),
    )


def _payment_read(payment) -> PaymentRead:
    return PaymentRead(
        id=payment.id,
        charge_id=payment.charge_id,
        amount=payment.amount,
        method=payment.method,
        paid_at=payment.paid_at,
        reference=payment.reference,
        receiver=payment.receiver,
        reconciliation_note=payment.reconciliation_note,
        verification_status=payment.verification_status,
        verified_at=payment.verified_at,
    )


def _payment_read_from_outcome(outcome: dict) -> PaymentRead:
    from datetime import datetime as _dt

    return PaymentRead(
        id=int(outcome["resource_id"]),
        charge_id=outcome["charge_id"],
        amount=Decimal(outcome["amount"]),
        method=outcome["method"],
        paid_at=_dt.fromisoformat(outcome["paid_at"]),
        reference=outcome.get("reference"),
        receiver=outcome.get("receiver"),
        reconciliation_note=outcome.get("reconciliation_note"),
        verification_status=outcome.get("verification_status", "unverified"),
        verified_at=(
            _dt.fromisoformat(outcome["verified_at"])
            if outcome.get("verified_at")
            else None
        ),
    )


def _follow_up_read(follow_up) -> ChargeFollowUpRead:
    return ChargeFollowUpRead(
        id=follow_up.id,
        charge_id=follow_up.charge_id,
        next_follow_up_on=follow_up.next_follow_up_on,
        note=follow_up.note,
        state=follow_up.state,
        opened_at=follow_up.opened_at,
        closed_at=follow_up.closed_at,
        close_reason=follow_up.close_reason,
        charge_amount=follow_up._fe3a_charge_amount,
        charge_paid=follow_up._fe3a_charge_paid,
        charge_outstanding=follow_up._fe3a_charge_outstanding,
        is_active_case=follow_up._fe3a_is_active_case,
        patient_id=follow_up._fe3a_patient_id,
        patient_name=follow_up._fe3a_patient_name,
        service_id=follow_up._fe3a_service_id,
        service_name=follow_up._fe3a_service_name,
        location_id=follow_up._fe3a_location_id,
        location_name=follow_up._fe3a_location_name,
    )


def _follow_up_read_from_outcome(outcome: dict) -> ChargeFollowUpRead:
    from datetime import datetime as _dt

    return ChargeFollowUpRead(
        id=int(outcome["resource_id"]),
        charge_id=outcome["charge_id"],
        next_follow_up_on=date.fromisoformat(outcome["next_follow_up_on"]),
        note=outcome.get("note"),
        state=outcome["state"],
        opened_at=_dt.fromisoformat(outcome["opened_at"]),
        closed_at=_dt.fromisoformat(outcome["closed_at"]) if outcome.get("closed_at") else None,
        close_reason=outcome.get("close_reason"),
        charge_amount=Decimal(outcome["charge_amount"]),
        charge_paid=Decimal(outcome["charge_paid"]),
        charge_outstanding=Decimal(outcome["charge_outstanding"]),
        is_active_case=outcome["is_active_case"],
        patient_id=outcome["patient_id"],
        patient_name=outcome["patient_name"],
        service_id=outcome["service_id"],
        service_name=outcome["service_name"],
        location_id=outcome["location_id"],
        location_name=outcome["location_name"],
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
    patient_id: int | None = None,
    location_id: int | None = None,
    visit_id: int | None = None,
    status: Literal["unpaid", "partial", "paid"] | None = None,
    created_from: date | None = Query(
        default=None, description="Inclusive lower bound on Charge.created_at."
    ),
    created_to: date | None = Query(
        default=None, description="Exclusive upper bound on Charge.created_at."
    ),
) -> list[ChargeRead]:
    ctx = resolve_http_context(request)
    charges = list_charges(
        db,
        ctx=ctx,
        execution_id=execution_id,
        patient_id=patient_id,
        location_id=location_id,
        visit_id=visit_id,
        status=status,
        created_from=created_from,
        created_to=created_to,
    )
    return [_charge_read(c, paid=getattr(c, "_fe3a_paid", Decimal("0"))) for c in charges]


@router.get("/charges/{charge_id}", response_model=ChargeRead)
def get_charge_route(
    charge_id: int, request: Request, db: Session = Depends(get_db)
) -> ChargeRead:
    ctx = resolve_http_context(request)
    charge = get_charge(db, charge_id, ctx=ctx)
    return _charge_read(charge, paid=getattr(charge, "_fe3a_paid", Decimal("0")))


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


@router.get("/payments", response_model=list[PaymentRead])
def list_all_payments_route(
    request: Request,
    db: Session = Depends(get_db),
    charge_id: int | None = None,
    method: PaymentMethod | None = None,
    verification_status: Literal["unverified", "verified"] | None = None,
    paid_from: date | None = Query(
        default=None, description="Inclusive lower bound on Payment.paid_at."
    ),
    paid_to: date | None = Query(
        default=None, description="Exclusive upper bound on Payment.paid_at."
    ),
) -> list[PaymentRead]:
    ctx = resolve_http_context(request)
    return [
        _payment_read(payment)
        for payment in list_all_payments(
            db,
            ctx=ctx,
            charge_id=charge_id,
            method=method,
            verification_status=verification_status,
            paid_from=paid_from,
            paid_to=paid_to,
        )
    ]


@router.post("/payments/{payment_id}/verify", response_model=PaymentRead)
def verify_payment_route(
    payment_id: int,
    payload: PaymentVerify,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> PaymentRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=verify_payment,
        operation_name=OP_PAYMENTS_VERIFY,
        key=key,
        ctx=ctx,
        params={"payment_id": payment_id, **payload.model_dump()},
        payment_id=payment_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _payment_read_from_outcome(outcome.outcome)
    return _payment_read(outcome.result)


@router.post(
    "/charges/{charge_id}/follow-ups",
    response_model=ChargeFollowUpRead,
    status_code=201,
)
def open_follow_up_route(
    charge_id: int,
    payload: ChargeFollowUpCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ChargeFollowUpRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=open_follow_up,
        operation_name=OP_FOLLOW_UPS_CREATE,
        key=key,
        ctx=ctx,
        params={"charge_id": charge_id, **payload.model_dump()},
        charge_id=charge_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _follow_up_read_from_outcome(outcome.outcome)
    return _follow_up_read(outcome.result)


@router.get(
    "/charges/{charge_id}/follow-ups",
    response_model=list[ChargeFollowUpRead],
)
def list_charge_follow_ups_route(
    charge_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> list[ChargeFollowUpRead]:
    ctx = resolve_http_context(request)
    return [_follow_up_read(row) for row in list_charge_follow_ups(db, charge_id, ctx=ctx)]


@router.get("/follow-ups", response_model=list[ChargeFollowUpRead])
def list_follow_ups_route(
    request: Request,
    db: Session = Depends(get_db),
    state: Literal["open", "closed"] | None = None,
    active: bool | None = None,
    due_on_or_before: date | None = None,
    patient_id: int | None = None,
    location_id: int | None = None,
) -> list[ChargeFollowUpRead]:
    ctx = resolve_http_context(request)
    return [
        _follow_up_read(row)
        for row in list_follow_ups(
            db,
            ctx=ctx,
            state=state,
            active=active,
            due_on_or_before=due_on_or_before,
            patient_id=patient_id,
            location_id=location_id,
        )
    ]


@router.post("/follow-ups/{follow_up_id}/reschedule", response_model=ChargeFollowUpRead)
def reschedule_follow_up_route(
    follow_up_id: int,
    payload: ChargeFollowUpReschedule,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ChargeFollowUpRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=reschedule_follow_up,
        operation_name=OP_FOLLOW_UPS_RESCHEDULE,
        key=key,
        ctx=ctx,
        params={"follow_up_id": follow_up_id, **payload.model_dump()},
        follow_up_id=follow_up_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _follow_up_read_from_outcome(outcome.outcome)
    return _follow_up_read(outcome.result)


@router.post("/follow-ups/{follow_up_id}/close", response_model=ChargeFollowUpRead)
def close_follow_up_route(
    follow_up_id: int,
    payload: ChargeFollowUpClose,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> ChargeFollowUpRead:
    ctx = resolve_http_context(request)
    key = _idempotency_key(request)
    outcome = run_idempotent_command(
        db,
        operation=close_follow_up,
        operation_name=OP_FOLLOW_UPS_CLOSE,
        key=key,
        ctx=ctx,
        params={"follow_up_id": follow_up_id, **payload.model_dump()},
        follow_up_id=follow_up_id,
        data=payload,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
        return _follow_up_read_from_outcome(outcome.outcome)
    return _follow_up_read(outcome.result)
