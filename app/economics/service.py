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

from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.catalog.models import Service
from app.clinical.models import Patient, ServiceExecution, Visit
from app.context import default_context
from app.economics.models import Charge, ChargeFollowUp, Payment, Product, ServiceConsumption
from app.economics.schemas import (
    ChargeCreate,
    ChargeFollowUpClose,
    ChargeFollowUpCreate,
    ChargeFollowUpReschedule,
    PaymentCreate,
    PaymentVerify,
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
    PAYMENTS_MANAGE,
    PAYMENTS_READ,
    FOLLOW_UPS_CREATE,
    FOLLOW_UPS_MANAGE,
    FOLLOW_UPS_READ,
    PRODUCTS_CREATE,
    PRODUCTS_READ,
)
from app.iam.service import require_permission
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.inventory.models import InventoryMovement, SALIDA
from app.inventory.service import require_stock
from app.organization.models import Location, Practitioner
from app.tenancy import scoped

OP_PRODUCTS_CREATE = "products.create"
OP_CONSUMPTIONS_CREATE = "consumptions.create"
OP_CHARGES_CREATE = "charges.create"
OP_PAYMENTS_CREATE = "payments.create"
OP_PAYMENTS_VERIFY = "payments.verify"
OP_FOLLOW_UPS_CREATE = "follow_ups.create"
OP_FOLLOW_UPS_RESCHEDULE = "follow_ups.reschedule"
OP_FOLLOW_UPS_CLOSE = "follow_ups.close"

PRODUCT_ENTITY_TYPE = "product"
CONSUMPTION_ENTITY_TYPE = "service_consumption"
CHARGE_ENTITY_TYPE = "charge"
PAYMENT_ENTITY_TYPE = "payment"
PRODUCT_CREATED_ACTION = "product.created"
CONSUMPTION_CREATED_ACTION = "service_consumption.created"
CHARGE_CREATED_ACTION = "charge.created"
PAYMENT_CREATED_ACTION = "payment.created"
PAYMENT_VERIFIED_ACTION = "payment.verified"
FOLLOW_UP_OPENED_ACTION = "charge_follow_up.opened"
FOLLOW_UP_RESCHEDULED_ACTION = "charge_follow_up.rescheduled"
FOLLOW_UP_CLOSED_ACTION = "charge_follow_up.closed"


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


def _utc_date_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _charge_projection_rows(
    session: Session,
    organization_id: int,
    *,
    charge_id: int | None = None,
    execution_id: int | None = None,
    patient_id: int | None = None,
    location_id: int | None = None,
    visit_id: int | None = None,
    status: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> list[Charge]:
    """Load charges with the bounded clinical context required by BE-1."""
    paid_expr = (
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.organization_id == Charge.organization_id,
            Payment.charge_id == Charge.id,
        )
        .correlate(Charge)
        .scalar_subquery()
    )
    statement = (
        select(Charge, ServiceExecution, Visit, Patient, Service, Location, Practitioner, paid_expr)
        .join(
            ServiceExecution,
            and_(
                ServiceExecution.organization_id == Charge.organization_id,
                ServiceExecution.id == Charge.service_execution_id,
            ),
        )
        .join(
            Visit,
            and_(
                Visit.organization_id == ServiceExecution.organization_id,
                Visit.id == ServiceExecution.visit_id,
            ),
        )
        .join(
            Patient,
            and_(Patient.organization_id == Visit.organization_id, Patient.id == Visit.patient_id),
        )
        .join(
            Service,
            and_(
                Service.organization_id == ServiceExecution.organization_id,
                Service.id == ServiceExecution.service_id,
            ),
        )
        .join(
            Location,
            and_(Location.organization_id == Visit.organization_id, Location.id == Visit.location_id),
        )
        .join(Practitioner, Practitioner.id == Visit.practitioner_id)
        .where(Charge.organization_id == organization_id)
        .order_by(Charge.created_at.desc(), Charge.id.desc())
    )
    if charge_id is not None:
        statement = statement.where(Charge.id == charge_id)
    if execution_id is not None:
        statement = statement.where(Charge.service_execution_id == execution_id)
    if patient_id is not None:
        statement = statement.where(Visit.patient_id == patient_id)
    if location_id is not None:
        statement = statement.where(Visit.location_id == location_id)
    if visit_id is not None:
        statement = statement.where(ServiceExecution.visit_id == visit_id)
    if status == "unpaid":
        statement = statement.where(paid_expr == 0)
    elif status == "partial":
        statement = statement.where(paid_expr > 0, paid_expr < Charge.amount)
    elif status == "paid":
        statement = statement.where(paid_expr == Charge.amount)
    if created_from is not None:
        statement = statement.where(Charge.created_at >= _utc_date_start(created_from))
    if created_to is not None:
        statement = statement.where(Charge.created_at < _utc_date_start(created_to))

    charges: list[Charge] = []
    for charge, execution, visit, patient, service, location, practitioner, paid in session.execute(
        statement
    ).all():
        charge._fe3a_visit_id = visit.id
        charge._fe3a_patient_id = patient.id
        charge._fe3a_patient_name = patient.full_name
        charge._fe3a_service_id = service.id
        charge._fe3a_service_name = service.name
        charge._fe3a_location_id = location.id
        charge._fe3a_location_name = location.name
        charge._fe3a_practitioner_id = practitioner.id
        charge._fe3a_practitioner_name = practitioner.display_name
        charge._fe3a_executed_at = execution.executed_at
        charge._fe3a_paid = Decimal(paid or 0)
        charges.append(charge)
    return charges


def _attach_charge_projection(session: Session, charge: Charge, organization_id: int) -> Charge:
    rows = _charge_projection_rows(
        session,
        organization_id,
        charge_id=charge.id,
    )
    if rows:
        return rows[0]
    return charge


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

        _attach_charge_projection(session, charge, org_id)
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
                "visit_id": charge._fe3a_visit_id,
                "patient_id": charge._fe3a_patient_id,
                "patient_name": charge._fe3a_patient_name,
                "service_id": charge._fe3a_service_id,
                "service_name": charge._fe3a_service_name,
                "location_id": charge._fe3a_location_id,
                "location_name": charge._fe3a_location_name,
                "practitioner_id": charge._fe3a_practitioner_id,
                "practitioner_name": charge._fe3a_practitioner_name,
                "executed_at": charge._fe3a_executed_at.isoformat(),
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
    return _attach_charge_projection(session, _load_charge(session, charge_id, org_id), org_id)


def list_charges(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    execution_id: int | None = None,
    patient_id: int | None = None,
    location_id: int | None = None,
    visit_id: int | None = None,
    status: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> list[Charge]:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, CHARGES_READ)
    if execution_id is not None:
        _load_execution(session, execution_id, org_id)
    return _charge_projection_rows(
        session,
        org_id,
        execution_id=execution_id,
        patient_id=patient_id,
        location_id=location_id,
        visit_id=visit_id,
        status=status,
        created_from=created_from,
        created_to=created_to,
    )


# --- Payment ----------------------------------------------------------------


def _payment_outcome(payment: Payment) -> dict:
    return {
        "status": "applied",
        "resource_type": PAYMENT_ENTITY_TYPE,
        "resource_id": str(payment.id),
        "charge_id": payment.charge_id,
        "amount": str(payment.amount),
        "method": payment.method,
        "paid_at": payment.paid_at.isoformat(),
        "reference": payment.reference,
        "receiver": payment.receiver,
        "reconciliation_note": payment.reconciliation_note,
        "verification_status": payment.verification_status,
        "verified_at": payment.verified_at.isoformat() if payment.verified_at else None,
    }


DUPLICATE_PAYMENT_REFERENCE_INDEX = "uq_payments_org_method_reference"


def _is_duplicate_payment_reference(exc: IntegrityError) -> bool:
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
    return diag is not None and getattr(diag, "constraint_name", None) == DUPLICATE_PAYMENT_REFERENCE_INDEX


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
            reference=(
                getattr(data, "reference", None).strip()
                if getattr(data, "reference", None) is not None
                else None
            ),
            receiver=getattr(data, "receiver", None),
            reconciliation_note=getattr(data, "reconciliation_note", None),
            verification_status="unverified",
        )
        session.add(payment)
        try:
            session.flush()
        except IntegrityError as exc:
            if _is_duplicate_payment_reference(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "A payment with that operation code already exists.",
                ) from exc
            raise

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
                "reference": payment.reference,
                "receiver": payment.receiver,
                "reconciliation_note": payment.reconciliation_note,
                "verification_status": payment.verification_status,
                "verified_at": payment.verified_at.isoformat() if payment.verified_at else None,
            },
        )

        if paid + payment.amount == charge.amount:
            _settle_open_follow_up(session, charge, resolved)

        settle_receipt(
            receipt,
            resource_type=PAYMENT_ENTITY_TYPE,
            resource_id=str(payment.id),
            outcome_json=_payment_outcome(payment),
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


def list_all_payments(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    charge_id: int | None = None,
    method: str | None = None,
    verification_status: str | None = None,
    paid_from: date | None = None,
    paid_to: date | None = None,
) -> list[Payment]:
    """Tenant-scoped reconciliation worklist ordered newest first."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, PAYMENTS_READ)
    statement = (
        scoped(select(Payment), Payment, org_id)
        .order_by(Payment.paid_at.desc(), Payment.id.desc())
    )
    if charge_id is not None:
        _load_charge(session, charge_id, org_id)
        statement = statement.where(Payment.charge_id == charge_id)
    if method is not None:
        statement = statement.where(Payment.method == method)
    if verification_status is not None:
        statement = statement.where(Payment.verification_status == verification_status)
    if paid_from is not None:
        statement = statement.where(Payment.paid_at >= _utc_date_start(paid_from))
    if paid_to is not None:
        statement = statement.where(Payment.paid_at < _utc_date_start(paid_to))
    return list(session.scalars(statement))


def verify_payment(
    session: Session,
    payment_id: int,
    data: PaymentVerify,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> Payment:
    """One-way verification of a recorded payment."""
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id

    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, PAYMENTS_MANAGE)
        payment = session.scalar(
            scoped(select(Payment).where(Payment.id == payment_id), Payment, org_id)
            .with_for_update()
        )
        if payment is None:
            raise AppError(ErrorCode.NOT_FOUND, "Payment not found.")
        if payment.verification_status == "verified":
            raise AppError(ErrorCode.INVALID_INPUT, "The payment is already verified.")

        before_state = {
            "id": payment.id,
            "charge_id": payment.charge_id,
            "verification_status": payment.verification_status,
            "verified_at": None,
            "reconciliation_note": payment.reconciliation_note,
        }
        payment.verification_status = "verified"
        payment.verified_at = datetime.now(timezone.utc)
        if data.reconciliation_note is not None:
            payment.reconciliation_note = data.reconciliation_note
        session.flush()
        record_event(
            session,
            ctx=resolved,
            entity_type=PAYMENT_ENTITY_TYPE,
            entity_id=str(payment.id),
            action=PAYMENT_VERIFIED_ACTION,
            before_state=before_state,
            after_state={
                "id": payment.id,
                "charge_id": payment.charge_id,
                "verification_status": payment.verification_status,
                "verified_at": payment.verified_at.isoformat(),
                "reconciliation_note": payment.reconciliation_note,
            },
        )
        settle_receipt(
            receipt,
            resource_type=PAYMENT_ENTITY_TYPE,
            resource_id=str(payment.id),
            outcome_json=_payment_outcome(payment),
        )
    return payment


# --- Collection follow-ups --------------------------------------------------


def _follow_up_state(follow_up: ChargeFollowUp) -> dict:
    return {
        "id": follow_up.id,
        "charge_id": follow_up.charge_id,
        "next_follow_up_on": follow_up.next_follow_up_on.isoformat(),
        "note": follow_up.note,
        "state": follow_up.state,
        "opened_at": follow_up.opened_at.isoformat(),
        "closed_at": follow_up.closed_at.isoformat() if follow_up.closed_at else None,
        "close_reason": follow_up.close_reason,
    }


def _follow_up_outcome(follow_up: ChargeFollowUp) -> dict:
    return {
        "status": "applied",
        "resource_type": "charge_follow_up",
        "resource_id": str(follow_up.id),
        "charge_id": follow_up.charge_id,
        "next_follow_up_on": follow_up.next_follow_up_on.isoformat(),
        "note": follow_up.note,
        "state": follow_up.state,
        "opened_at": follow_up.opened_at.isoformat(),
        "closed_at": follow_up.closed_at.isoformat() if follow_up.closed_at else None,
        "close_reason": follow_up.close_reason,
        "charge_amount": str(follow_up._fe3a_charge_amount),
        "charge_paid": str(follow_up._fe3a_charge_paid),
        "charge_outstanding": str(follow_up._fe3a_charge_outstanding),
        "is_active_case": follow_up._fe3a_is_active_case,
        "patient_id": follow_up._fe3a_patient_id,
        "patient_name": follow_up._fe3a_patient_name,
        "service_id": follow_up._fe3a_service_id,
        "service_name": follow_up._fe3a_service_name,
        "location_id": follow_up._fe3a_location_id,
        "location_name": follow_up._fe3a_location_name,
    }


def _follow_up_projection_rows(
    session: Session,
    organization_id: int,
    *,
    follow_up_id: int | None = None,
    charge_id: int | None = None,
    state: str | None = None,
    active: bool | None = None,
    due_on_or_before: date | None = None,
    patient_id: int | None = None,
    location_id: int | None = None,
    order_opened_desc: bool = False,
) -> list[ChargeFollowUp]:
    paid_expr = (
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.organization_id == Charge.organization_id,
            Payment.charge_id == Charge.id,
        )
        .correlate(Charge)
        .scalar_subquery()
    )
    outstanding_expr = Charge.amount - paid_expr
    active_expr = and_(ChargeFollowUp.state == "open", outstanding_expr > 0)
    statement = (
        select(
            ChargeFollowUp,
            Charge,
            ServiceExecution,
            Visit,
            Patient,
            Service,
            Location,
            paid_expr,
        )
        .join(
            Charge,
            and_(
                Charge.organization_id == ChargeFollowUp.organization_id,
                Charge.id == ChargeFollowUp.charge_id,
            ),
        )
        .join(
            ServiceExecution,
            and_(
                ServiceExecution.organization_id == Charge.organization_id,
                ServiceExecution.id == Charge.service_execution_id,
            ),
        )
        .join(
            Visit,
            and_(
                Visit.organization_id == ServiceExecution.organization_id,
                Visit.id == ServiceExecution.visit_id,
            ),
        )
        .join(
            Patient,
            and_(Patient.organization_id == Visit.organization_id, Patient.id == Visit.patient_id),
        )
        .join(
            Service,
            and_(
                Service.organization_id == ServiceExecution.organization_id,
                Service.id == ServiceExecution.service_id,
            ),
        )
        .join(
            Location,
            and_(Location.organization_id == Visit.organization_id, Location.id == Visit.location_id),
        )
        .where(ChargeFollowUp.organization_id == organization_id)
    )
    if order_opened_desc:
        statement = statement.order_by(
            ChargeFollowUp.opened_at.desc(), ChargeFollowUp.id.desc()
        )
    else:
        statement = statement.order_by(
            ChargeFollowUp.next_follow_up_on.asc(), ChargeFollowUp.id.asc()
        )
    if follow_up_id is not None:
        statement = statement.where(ChargeFollowUp.id == follow_up_id)
    if charge_id is not None:
        statement = statement.where(ChargeFollowUp.charge_id == charge_id)
    if state is not None:
        statement = statement.where(ChargeFollowUp.state == state)
    if active is True:
        statement = statement.where(active_expr)
    elif active is False:
        statement = statement.where(~active_expr)
    if due_on_or_before is not None:
        statement = statement.where(ChargeFollowUp.next_follow_up_on <= due_on_or_before)
    if patient_id is not None:
        statement = statement.where(Visit.patient_id == patient_id)
    if location_id is not None:
        statement = statement.where(Visit.location_id == location_id)

    rows: list[ChargeFollowUp] = []
    for follow_up, charge, execution, visit, patient, service, location, paid in session.execute(
        statement
    ).all():
        paid_amount = Decimal(paid or 0)
        follow_up._fe3a_charge_amount = charge.amount
        follow_up._fe3a_charge_paid = paid_amount
        follow_up._fe3a_charge_outstanding = charge.amount - paid_amount
        follow_up._fe3a_is_active_case = (
            follow_up.state == "open" and follow_up._fe3a_charge_outstanding > 0
        )
        follow_up._fe3a_patient_id = patient.id
        follow_up._fe3a_patient_name = patient.full_name
        follow_up._fe3a_service_id = service.id
        follow_up._fe3a_service_name = service.name
        follow_up._fe3a_location_id = location.id
        follow_up._fe3a_location_name = location.name
        rows.append(follow_up)
    return rows


def _attach_follow_up_projection(
    session: Session, follow_up: ChargeFollowUp, organization_id: int
) -> ChargeFollowUp:
    rows = _follow_up_projection_rows(
        session,
        organization_id,
        follow_up_id=follow_up.id,
    )
    return rows[0] if rows else follow_up


def _charge_local_today(session: Session, charge: Charge) -> date:
    projected = _attach_charge_projection(session, charge, charge.organization_id)
    location = session.get(Location, projected._fe3a_location_id)
    timezone_name = location.timezone if location is not None else "America/Lima"
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:  # pragma: no cover - database validation owns this value
        zone = ZoneInfo("America/Lima")
    return datetime.now(zone).date()


def _validate_follow_up_date(session: Session, charge: Charge, value: date) -> None:
    if value < _charge_local_today(session, charge):
        raise AppError(ErrorCode.INVALID_INPUT, "next_follow_up_on cannot be in the past.")


def _append_follow_up_note(existing: str | None, addition: str | None) -> str | None:
    if addition is None:
        return existing
    value = f"{existing}\n{addition}" if existing else addition
    if len(value) > 500:
        raise AppError(ErrorCode.INVALID_INPUT, "The follow-up note is too long.")
    return value


def _settle_open_follow_up(
    session: Session, charge: Charge, resolved: ExecutionContext
) -> ChargeFollowUp | None:
    follow_up = session.scalar(
        select(ChargeFollowUp)
        .where(
            ChargeFollowUp.organization_id == charge.organization_id,
            ChargeFollowUp.charge_id == charge.id,
            ChargeFollowUp.state == "open",
        )
        .with_for_update()
    )
    if follow_up is None:
        return None
    before_state = _follow_up_state(follow_up)
    follow_up.state = "closed"
    follow_up.closed_at = datetime.now(timezone.utc)
    follow_up.close_reason = "settled"
    session.flush()
    record_event(
        session,
        ctx=resolved,
        entity_type="charge_follow_up",
        entity_id=str(follow_up.id),
        action=FOLLOW_UP_CLOSED_ACTION,
        before_state=before_state,
        after_state=_follow_up_state(follow_up),
    )
    return follow_up


def open_follow_up(
    session: Session,
    charge_id: int,
    data: ChargeFollowUpCreate,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> ChargeFollowUp:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, FOLLOW_UPS_CREATE)
        charge = session.scalar(
            scoped(select(Charge).where(Charge.id == charge_id), Charge, org_id)
            .with_for_update()
        )
        if charge is None:
            raise AppError(ErrorCode.NOT_FOUND, "Charge not found.")
        paid = charge_paid_amount(session, charge.id, org_id)
        if charge.amount - paid <= 0:
            raise AppError(ErrorCode.INVALID_INPUT, "The charge is already fully paid.")
        _validate_follow_up_date(session, charge, data.next_follow_up_on)
        existing = session.scalar(
            select(ChargeFollowUp).where(
                ChargeFollowUp.organization_id == org_id,
                ChargeFollowUp.charge_id == charge.id,
                ChargeFollowUp.state == "open",
            )
        )
        if existing is not None:
            raise AppError(ErrorCode.INVALID_INPUT, "The charge already has an open follow-up.")
        follow_up = ChargeFollowUp(
            organization_id=org_id,
            charge_id=charge.id,
            next_follow_up_on=data.next_follow_up_on,
            note=data.note,
            state="open",
        )
        session.add(follow_up)
        try:
            session.flush()
        except IntegrityError as exc:
            if _is_duplicate_follow_up_open(exc):
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The charge already has an open follow-up.",
                ) from exc
            raise
        before_state = None
        record_event(
            session,
            ctx=resolved,
            entity_type="charge_follow_up",
            entity_id=str(follow_up.id),
            action=FOLLOW_UP_OPENED_ACTION,
            before_state=before_state,
            after_state=_follow_up_state(follow_up),
        )
        _attach_follow_up_projection(session, follow_up, org_id)
        settle_receipt(
            receipt,
            resource_type="charge_follow_up",
            resource_id=str(follow_up.id),
            outcome_json=_follow_up_outcome(follow_up),
        )
    return follow_up


DUPLICATE_FOLLOW_UP_OPEN_INDEX = "uq_charge_follow_ups_org_charge_open"


def _is_duplicate_follow_up_open(exc: IntegrityError) -> bool:
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
    return diag is not None and getattr(diag, "constraint_name", None) == DUPLICATE_FOLLOW_UP_OPEN_INDEX


def _lock_follow_up(session: Session, follow_up_id: int, organization_id: int) -> ChargeFollowUp:
    follow_up = session.scalar(
        scoped(select(ChargeFollowUp).where(ChargeFollowUp.id == follow_up_id), ChargeFollowUp, organization_id)
        .with_for_update()
    )
    if follow_up is None:
        raise AppError(ErrorCode.NOT_FOUND, "Follow-up not found.")
    return follow_up


def reschedule_follow_up(
    session: Session,
    follow_up_id: int,
    data: ChargeFollowUpReschedule,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> ChargeFollowUp:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, FOLLOW_UPS_MANAGE)
        follow_up = _lock_follow_up(session, follow_up_id, org_id)
        if follow_up.state != "open":
            raise AppError(ErrorCode.ENTITY_INACTIVE, "The follow-up is closed.")
        charge = _load_charge(session, follow_up.charge_id, org_id)
        _validate_follow_up_date(session, charge, data.next_follow_up_on)
        before_state = _follow_up_state(follow_up)
        follow_up.next_follow_up_on = data.next_follow_up_on
        follow_up.note = data.note
        session.flush()
        record_event(
            session,
            ctx=resolved,
            entity_type="charge_follow_up",
            entity_id=str(follow_up.id),
            action=FOLLOW_UP_RESCHEDULED_ACTION,
            before_state=before_state,
            after_state=_follow_up_state(follow_up),
        )
        _attach_follow_up_projection(session, follow_up, org_id)
        settle_receipt(
            receipt,
            resource_type="charge_follow_up",
            resource_id=str(follow_up.id),
            outcome_json=_follow_up_outcome(follow_up),
        )
    return follow_up


def close_follow_up(
    session: Session,
    follow_up_id: int,
    data: ChargeFollowUpClose,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    idempotency: IdempotencyClaim | None = None,
) -> ChargeFollowUp:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    with session.begin():
        receipt = claim_receipt(session, resolved, idempotency)
        if ctx is not None:
            require_permission(session, resolved, FOLLOW_UPS_MANAGE)
        follow_up = _lock_follow_up(session, follow_up_id, org_id)
        if follow_up.state != "open":
            raise AppError(ErrorCode.ENTITY_INACTIVE, "The follow-up is closed.")
        before_state = _follow_up_state(follow_up)
        follow_up.state = "closed"
        follow_up.closed_at = datetime.now(timezone.utc)
        follow_up.close_reason = "closed_by_operator"
        follow_up.note = _append_follow_up_note(follow_up.note, data.note)
        session.flush()
        record_event(
            session,
            ctx=resolved,
            entity_type="charge_follow_up",
            entity_id=str(follow_up.id),
            action=FOLLOW_UP_CLOSED_ACTION,
            before_state=before_state,
            after_state=_follow_up_state(follow_up),
        )
        _attach_follow_up_projection(session, follow_up, org_id)
        settle_receipt(
            receipt,
            resource_type="charge_follow_up",
            resource_id=str(follow_up.id),
            outcome_json=_follow_up_outcome(follow_up),
        )
    return follow_up


def list_charge_follow_ups(
    session: Session,
    charge_id: int,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
) -> list[ChargeFollowUp]:
    resolved = _resolved_context(ctx, organization_id)
    if ctx is not None:
        require_permission(session, resolved, FOLLOW_UPS_READ)
    _load_charge(session, charge_id, resolved.organization_id)
    return _follow_up_projection_rows(
        session,
        resolved.organization_id,
        charge_id=charge_id,
        order_opened_desc=True,
    )


def list_follow_ups(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    state: str | None = None,
    active: bool | None = None,
    due_on_or_before: date | None = None,
    patient_id: int | None = None,
    location_id: int | None = None,
) -> list[ChargeFollowUp]:
    resolved = _resolved_context(ctx, organization_id)
    if ctx is not None:
        require_permission(session, resolved, FOLLOW_UPS_READ)
    return _follow_up_projection_rows(
        session,
        resolved.organization_id,
        state=state,
        active=active,
        due_on_or_before=due_on_or_before,
        patient_id=patient_id,
        location_id=location_id,
    )
