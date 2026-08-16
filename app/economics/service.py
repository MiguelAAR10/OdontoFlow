"""Economic & operations application services (Product, Consumption, Charge, Payment).

Every command follows the module conventions: idle Session + explicit
ExecutionContext (PF3), tenant from the context (X3), ``require_permission``
first after the claim (E6), audit staged in the same transaction (PF3), and
PF4 claim-first idempotency (§16.1) via the shared claim/settle helpers.

Authority: ``.audit/clinical-core/next-economic-ops-contract.md``. The money
invariant (payments never exceed the charge) is enforced on the authoritative
mutation path: recording a payment takes the charge row ``FOR UPDATE``, which
serializes concurrent payments of the same charge deterministically.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.clinical.models import ServiceExecution, Visit
from app.context import default_context
from app.economics.models import Charge, Payment, Product, ServiceConsumption
from app.economics.schemas import (
    ChargeCreate,
    PaymentCreate,
    ProductCreate,
    ServiceConsumptionCreate,
)
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    CHARGES_CREATE,
    CHARGES_READ,
    CONSUMPTIONS_CREATE,
    CONSUMPTIONS_READ,
    PAYMENTS_CREATE,
    PAYMENTS_READ,
    PRODUCTS_CREATE,
    PRODUCTS_READ,
)
from app.iam.service import require_permission
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.inventory.models import InventoryMovement, SALIDA
from app.inventory.service import require_stock
from app.tenancy import scoped

OP_PRODUCTS_CREATE = "products.create"
OP_CONSUMPTIONS_CREATE = "consumptions.create"
OP_CHARGES_CREATE = "charges.create"
OP_PAYMENTS_CREATE = "payments.create"

PRODUCT_ENTITY_TYPE = "product"
CONSUMPTION_ENTITY_TYPE = "service_consumption"
CHARGE_ENTITY_TYPE = "charge"
PAYMENT_ENTITY_TYPE = "payment"
PRODUCT_CREATED_ACTION = "product.created"
CONSUMPTION_CREATED_ACTION = "service_consumption.created"
CHARGE_CREATED_ACTION = "charge.created"
PAYMENT_CREATED_ACTION = "payment.created"


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    return ctx if ctx is not None else default_context(organization_id)


def _load_execution(
    session: Session, execution_id: int, organization_id: int
) -> ServiceExecution:
    execution = session.scalar(
        scoped(
            select(ServiceExecution).where(ServiceExecution.id == execution_id),
            ServiceExecution,
            organization_id,
        )
    )
    if execution is None:
        raise AppError(ErrorCode.NOT_FOUND, "ServiceExecution not found.")
    return execution


def _load_execution_visit(
    session: Session, execution: ServiceExecution, organization_id: int
) -> Visit:
    """The visit that realized the execution — the stock-out location (M4.2).

    The execution's composite FK pins the visit to the same organization, so
    the returned location can never belong to another tenant.
    """
    visit = session.scalar(
        scoped(select(Visit).where(Visit.id == execution.visit_id), Visit, organization_id)
    )
    if visit is None:
        raise AppError(ErrorCode.NOT_FOUND, "Visit not found.")
    return visit


def _load_active_product(
    session: Session, product_id: int, organization_id: int
) -> Product:
    product = session.scalar(
        scoped(select(Product).where(Product.id == product_id), Product, organization_id)
    )
    if product is None:
        raise AppError(ErrorCode.NOT_FOUND, "Product not found.")
    if not product.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Product is inactive.")
    return product


def _load_charge(session: Session, charge_id: int, organization_id: int) -> Charge:
    charge = session.scalar(
        scoped(select(Charge).where(Charge.id == charge_id), Charge, organization_id)
    )
    if charge is None:
        raise AppError(ErrorCode.NOT_FOUND, "Charge not found.")
    return charge


def charge_paid_amount(session: Session, charge_id: int, organization_id: int) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.organization_id == organization_id,
            Payment.charge_id == charge_id,
        )
    )
    return Decimal(total or 0)


# --- Product ----------------------------------------------------------------


def create_product(
    session: Session,
    data: ProductCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Product:
    """Register one organization-owned product catalog item."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, PRODUCTS_CREATE)

        duplicate = session.scalar(
            scoped(select(Product).where(Product.name == data.name), Product, org_id)
        )
        if duplicate is not None:
            raise AppError(ErrorCode.INVALID_INPUT, f"Ya existe un producto con ese nombre: {data.name}")

        product = Product(
            organization_id=org_id,
            name=data.name,
            unit=data.unit,
            kind=data.kind,
        )
        session.add(product)
        try:
            session.flush()
        except Exception as exc:  # noqa: BLE001 - constraint discrimination below
            from sqlalchemy.exc import IntegrityError

            if isinstance(exc, IntegrityError) and _is_duplicate_product(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    f"Ya existe un producto con ese nombre: {data.name}",
                ) from exc
            raise

        record_event(
            session,
            ctx=resolved,
            entity_type=PRODUCT_ENTITY_TYPE,
            entity_id=str(product.id),
            action=PRODUCT_CREATED_ACTION,
            after_state={"id": product.id, "name": product.name, "kind": product.kind},
        )
        settle_receipt(
            receipt,
            resource_type=PRODUCT_ENTITY_TYPE,
            resource_id=str(product.id),
            outcome_json={
                "status": "applied",
                "resource_type": PRODUCT_ENTITY_TYPE,
                "resource_id": str(product.id),
                "name": product.name,
                "unit": product.unit,
                "kind": product.kind,
            },
        )

    return product


def list_products(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    search: str | None = None,
    kind: str | None = None,
) -> list[Product]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PRODUCTS_READ)

    statement = scoped(select(Product), Product, org_id).order_by(Product.name)
    if search:
        statement = statement.where(Product.name.ilike(f"%{search}%"))
    if kind is not None:
        statement = statement.where(Product.kind == kind)
    return list(session.scalars(statement))


def get_product(
    session: Session,
    product_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> Product:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PRODUCTS_READ)
    product = session.scalar(
        scoped(select(Product).where(Product.id == product_id), Product, org_id)
    )
    if product is None:
        raise AppError(ErrorCode.NOT_FOUND, "Product not found.")
    return product


# --- ServiceConsumption -----------------------------------------------------


def create_service_consumption(
    session: Session,
    execution_id: int,
    data: ServiceConsumptionCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> ServiceConsumption:
    """Record one product actually used during an executed service.

    Actual clinical use, not an inventory balance change authority: the ledger
    (InventoryMovement) is a separate vertical. The unit price is the
    point-in-time snapshot; the line amount (quantity × unit_price) is derived
    at read. One product per execution line (``UNIQUE(org, execution,
    product)``), settled deterministically including under a concurrent race.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, CONSUMPTIONS_CREATE)

        execution = _load_execution(session, execution_id, org_id)
        _load_active_product(session, data.product_id, org_id)
        if data.quantity <= 0:
            raise AppError(ErrorCode.INVALID_INPUT, "quantity must be positive.")
        if data.unit_price < 0:
            raise AppError(ErrorCode.INVALID_INPUT, "unit_price must be non-negative.")

        duplicate = session.scalar(
            select(ServiceConsumption).where(
                ServiceConsumption.organization_id == org_id,
                ServiceConsumption.service_execution_id == execution_id,
                ServiceConsumption.product_id == data.product_id,
            )
        )
        if duplicate is not None:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The product is already consumed in this execution.",
            )

        # Inventory (PF7 + M4.2): the stock floor is the ledger sum of the
        # location where the service was actually performed (the execution's
        # visit location — never client-supplied); the SALIDA movement lands
        # atomically with the consumption row (1:1 via id_consumo_origen
        # UNIQUE) anchored to that same location.
        visit = _load_execution_visit(session, execution, org_id)
        stock_location_id = visit.location_id
        require_stock(
            session, data.product_id, org_id, data.quantity, stock_location_id
        )

        consumption = ServiceConsumption(
            organization_id=org_id,
            service_execution_id=execution_id,
            product_id=data.product_id,
            quantity=data.quantity,
            unit_price=data.unit_price,
        )
        session.add(consumption)
        try:
            session.flush()
        except Exception as exc:  # noqa: BLE001 - constraint discrimination below
            from sqlalchemy.exc import IntegrityError

            if isinstance(exc, IntegrityError) and _is_duplicate_consumption(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The product is already consumed in this execution.",
                ) from exc
            raise
        session.add(
            InventoryMovement(
                organization_id=org_id,
                product_id=data.product_id,
                location_id=stock_location_id,
                type=SALIDA,
                quantity=data.quantity,
                unit_price=data.unit_price,
                id_consumo_origen=consumption.id,
            )
        )
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=CONSUMPTION_ENTITY_TYPE,
            entity_id=str(consumption.id),
            action=CONSUMPTION_CREATED_ACTION,
            after_state={
                "id": consumption.id,
                "service_execution_id": consumption.service_execution_id,
                "product_id": consumption.product_id,
                "location_id": stock_location_id,
                "quantity": str(consumption.quantity),
                "unit_price": str(consumption.unit_price),
            },
        )
        settle_receipt(
            receipt,
            resource_type=CONSUMPTION_ENTITY_TYPE,
            resource_id=str(consumption.id),
            outcome_json={
                "status": "applied",
                "resource_type": CONSUMPTION_ENTITY_TYPE,
                "resource_id": str(consumption.id),
                "service_execution_id": consumption.service_execution_id,
                "product_id": consumption.product_id,
                "location_id": stock_location_id,
                "product_name": consumption.product.name,
                "quantity": str(consumption.quantity),
                "unit_price": str(consumption.unit_price),
                "consumed_at": consumption.consumed_at.isoformat(),
            },
        )

    return consumption


DUPLICATE_PRODUCT_CONSTRAINT = "uq_products_organization_name"


def _is_duplicate_product(exc) -> bool:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is None:
        diag = getattr(orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
    if str(sqlstate) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    if diag is None:
        return False
    return getattr(diag, "constraint_name", None) == DUPLICATE_PRODUCT_CONSTRAINT


DUPLICATE_CONSUMPTION_CONSTRAINT = "uq_service_consumptions_org_execution_product"


def _is_duplicate_consumption(exc) -> bool:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is None:
        diag = getattr(orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
    if str(sqlstate) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    if diag is None:
        return False
    return getattr(diag, "constraint_name", None) == DUPLICATE_CONSUMPTION_CONSTRAINT


def list_consumptions(
    session: Session,
    execution_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> list[ServiceConsumption]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, CONSUMPTIONS_READ)
    _load_execution(session, execution_id, org_id)
    return list(
        session.scalars(
            select(ServiceConsumption)
            .where(
                ServiceConsumption.organization_id == org_id,
                ServiceConsumption.service_execution_id == execution_id,
            )
            .order_by(ServiceConsumption.id)
        )
    )


# --- Charge -----------------------------------------------------------------


def create_charge(
    session: Session,
    execution_id: int,
    data: ChargeCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Charge:
    """Create the economic obligation of one performed service.

    The charged amount defaults to the execution's own price snapshot —
    never re-guessed from the catalog. One charge per execution
    (``UNIQUE(org, execution)``).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, CHARGES_CREATE)

        execution = _load_execution(session, execution_id, org_id)
        amount = data.amount if data.amount is not None else execution.executed_price
        if amount <= 0:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The charged amount must be positive.",
            )

        duplicate = session.scalar(
            select(Charge).where(
                Charge.organization_id == org_id,
                Charge.service_execution_id == execution_id,
            )
        )
        if duplicate is not None:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The execution already has a charge.",
            )

        charge = Charge(
            organization_id=org_id,
            service_execution_id=execution_id,
            amount=amount,
        )
        session.add(charge)
        try:
            session.flush()
        except Exception as exc:  # noqa: BLE001 - constraint discrimination below
            from sqlalchemy.exc import IntegrityError

            if isinstance(exc, IntegrityError) and _is_duplicate_charge(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The execution already has a charge.",
                ) from exc
            raise

        record_event(
            session,
            ctx=resolved,
            entity_type=CHARGE_ENTITY_TYPE,
            entity_id=str(charge.id),
            action=CHARGE_CREATED_ACTION,
            after_state={
                "id": charge.id,
                "service_execution_id": charge.service_execution_id,
                "amount": str(charge.amount),
            },
        )
        settle_receipt(
            receipt,
            resource_type=CHARGE_ENTITY_TYPE,
            resource_id=str(charge.id),
            outcome_json={
                "status": "applied",
                "resource_type": CHARGE_ENTITY_TYPE,
                "resource_id": str(charge.id),
                "service_execution_id": charge.service_execution_id,
                "amount": str(charge.amount),
                "created_at": charge.created_at.isoformat(),
            },
        )

    return charge


DUPLICATE_CHARGE_CONSTRAINT = "uq_charges_org_execution"


def _is_duplicate_charge(exc) -> bool:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is None:
        diag = getattr(orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
    if str(sqlstate) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    if diag is None:
        return False
    return getattr(diag, "constraint_name", None) == DUPLICATE_CHARGE_CONSTRAINT


def get_charge(
    session: Session,
    charge_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> Charge:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, CHARGES_READ)
    return _load_charge(session, charge_id, org_id)


def list_charges(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    execution_id: int | None = None,
) -> list[Charge]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, CHARGES_READ)
    statement = scoped(select(Charge), Charge, org_id).order_by(Charge.id)
    if execution_id is not None:
        _load_execution(session, execution_id, org_id)
        statement = statement.where(Charge.service_execution_id == execution_id)
    return list(session.scalars(statement))


# --- Payment ----------------------------------------------------------------


def create_payment(
    session: Session,
    charge_id: int,
    data: PaymentCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Payment:
    """Record one payment against a charge.

    The money invariant is enforced on this single authoritative path: the
    charge row is locked ``FOR UPDATE``, serializing concurrent payments of
    the same charge, and any payment that would exceed the outstanding amount
    is rejected deterministically (overpayment is structurally impossible).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, PAYMENTS_CREATE)

        charge = session.scalar(
            scoped(select(Charge).where(Charge.id == charge_id), Charge, org_id)
            .with_for_update()
        )
        if charge is None:
            raise AppError(ErrorCode.NOT_FOUND, "Charge not found.")

        paid = charge_paid_amount(session, charge.id, org_id)
        outstanding = charge.amount - paid
        if data.amount > outstanding:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The payment exceeds the outstanding amount of the charge.",
            )

        payment = Payment(
            organization_id=org_id,
            charge_id=charge.id,
            amount=data.amount,
            method=data.method,
        )
        session.add(payment)
        session.flush()

        record_event(
            session,
            ctx=resolved,
            entity_type=PAYMENT_ENTITY_TYPE,
            entity_id=str(payment.id),
            action=PAYMENT_CREATED_ACTION,
            after_state={
                "id": payment.id,
                "charge_id": payment.charge_id,
                "amount": str(payment.amount),
                "method": payment.method,
            },
        )
        settle_receipt(
            receipt,
            resource_type=PAYMENT_ENTITY_TYPE,
            resource_id=str(payment.id),
            outcome_json={
                "status": "applied",
                "resource_type": PAYMENT_ENTITY_TYPE,
                "resource_id": str(payment.id),
                "charge_id": payment.charge_id,
                "amount": str(payment.amount),
                "method": payment.method,
                "paid_at": payment.paid_at.isoformat(),
            },
        )

    return payment


def list_payments(
    session: Session,
    charge_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> list[Payment]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PAYMENTS_READ)
    _load_charge(session, charge_id, org_id)
    return list(
        session.scalars(
            select(Payment)
            .where(Payment.organization_id == org_id, Payment.charge_id == charge_id)
            .order_by(Payment.id)
        )
    )
