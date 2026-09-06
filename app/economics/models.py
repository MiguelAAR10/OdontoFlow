"""Economic & operations core: products, consumptions, charges, payments.

Four organization-owned tables with the §7 composite-FK pattern. Authority
per `.audit/clinical-core/next-economic-ops-contract.md`:
- Product declares its kind (CONSUMIBLE / REVENTA) — the legacy distinction
  was inferred from usage, never declared (defect #6 dropped);
- ServiceConsumption records actual clinical use anchored to one execution
  line, with quantity and the unit-price snapshot frozen at use time
  (``UNIQUE(org, execution, product)`` — one product per line);
- Charge is the economic obligation of one execution (1:1), amount charged
  defaulting to the execution's own price snapshot (never re-guessed);
- Payment belongs to a Charge (N:1), positive amount, deterministic
  overpayment rejection; outstanding amount is always derived, never stored.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

#: The declared product kinds (migration target for the legacy inferred split).
PRODUCT_KIND_CHECK = "kind IN ('consumible', 'reventa')"

PAYMENT_METHOD_CHECK = (
    "method IN ('efectivo', 'tarjeta', 'yape', 'plin', 'transferencia', 'link_pago')"
)
DIGITAL_METHODS = ("yape", "plin", "transferencia")
PAYMENT_VERIFICATION_STATUS_CHECK = "verification_status IN ('unverified', 'verified')"
PAYMENT_VERIFIED_AT_CHECK = (
    "(verification_status = 'unverified' AND verified_at IS NULL) OR "
    "(verification_status = 'verified' AND verified_at IS NOT NULL)"
)
FOLLOW_UP_STATE_CHECK = "state IN ('open', 'closed')"
FOLLOW_UP_CLOSE_REASON_CHECK = "close_reason IN ('settled', 'closed_by_operator')"
FOLLOW_UP_CLOSURE_CHECK = (
    "(state = 'open' AND closed_at IS NULL AND close_reason IS NULL) OR "
    "(state = 'closed' AND closed_at IS NOT NULL AND close_reason IS NOT NULL)"
)


class Product(Base):
    """An organization-owned product catalog item.

    No stock authority here: ``InventoryBalance``/``InventoryMovement`` are a
    separate vertical (see the next-inventory contract). Only the catalog
    semantics evidenced by the legacy domain are preserved: a name unique per
    organization, a unit of measure, and a declared kind.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_products_organization"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(PRODUCT_KIND_CHECK, name="ck_products_kind"),
        UniqueConstraint("organization_id", "name", name="uq_products_organization_name"),
        UniqueConstraint("organization_id", "id", name="uq_products_organization_id"),
    )


class ServiceConsumption(Base):
    """One product actually used during one executed service.

    Anchored to a ServiceExecution (legacy: ``consumo_productos`` → line) and
    to one canonical Product; ``quantity`` is positive and ``unit_price`` is
    the point-in-time snapshot frozen at use (the line amount is derived at
    read: ``quantity × unit_price``). A product is consumed at most once per
    execution line (``UNIQUE(org, execution, product)``).
    """

    __tablename__ = "service_consumptions"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_service_consumptions_organization",
        ),
        nullable=False,
    )
    service_execution_id: Mapped[int] = mapped_column(nullable=False)
    product_id: Mapped[int] = mapped_column(nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    execution: Mapped["ServiceExecution"] = relationship(  # noqa: F821
        foreign_keys=[service_execution_id],
        primaryjoin="ServiceConsumption.service_execution_id == ServiceExecution.id",
    )
    product: Mapped[Product] = relationship(
        foreign_keys=[product_id],
        primaryjoin="ServiceConsumption.product_id == Product.id",
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_service_consumptions_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_service_consumptions_price"),
        UniqueConstraint("organization_id", "id", name="uq_service_consumptions_organization_id"),
        UniqueConstraint(
            "organization_id",
            "service_execution_id",
            "product_id",
            name="uq_service_consumptions_org_execution_product",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_execution_id"],
            ["service_executions.organization_id", "service_executions.id"],
            ondelete="RESTRICT",
            name="fk_service_consumptions_organization_execution",
        ),
        ForeignKeyConstraint(
            ["organization_id", "product_id"],
            ["products.organization_id", "products.id"],
            ondelete="RESTRICT",
            name="fk_service_consumptions_organization_product",
        ),
    )


class Charge(Base):
    """The economic obligation created from one performed service.

    One charge per execution (``UNIQUE(org, execution)`` — the legacy factura
    1:1 adapted to the execution line). The charged amount defaults to the
    execution's own price snapshot (never re-guessed from the catalog). Paid
    and outstanding amounts are always derived from the payment rows.
    """

    __tablename__ = "charges"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_charges_organization"),
        nullable=False,
    )
    service_execution_id: Mapped[int] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    execution: Mapped["ServiceExecution"] = relationship(  # noqa: F821
        foreign_keys=[service_execution_id],
        primaryjoin="Charge.service_execution_id == ServiceExecution.id",
    )
    # Append-only by contract (legacy delete-orphan dropped; reversal, never
    # delete): no cascade here — the FK is RESTRICT and history is immutable.
    payments: Mapped[list["Payment"]] = relationship(back_populates="charge")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_charges_amount"),
        UniqueConstraint("organization_id", "id", name="uq_charges_organization_id"),
        UniqueConstraint(
            "organization_id",
            "service_execution_id",
            name="uq_charges_org_execution",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_execution_id"],
            ["service_executions.organization_id", "service_executions.id"],
            ondelete="RESTRICT",
            name="fk_charges_organization_execution",
        ),
    )


class Payment(Base):
    """One payment against a Charge.

    Multiple payments per charge are possible (legacy ``pagos`` N:1). The
    amount is positive and the sum of payments can never exceed the charge:
    the application serializes payments per charge with a row lock (one
    authoritative mutation path) and rejects overpayment deterministically.
    ``method`` is a closed Spanish wire-code vocabulary. Digital methods carry
    optional historical references, while new writes are validated at the API
    boundary and by the database check.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_payments_organization"),
        nullable=False,
    )
    charge_id: Mapped[int] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(60), nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reconciliation_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unverified"
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    charge: Mapped[Charge] = relationship(back_populates="payments")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount"),
        CheckConstraint(PAYMENT_METHOD_CHECK, name="ck_payments_method"),
        CheckConstraint(
            PAYMENT_VERIFICATION_STATUS_CHECK,
            name="ck_payments_verification_status",
        ),
        CheckConstraint(PAYMENT_VERIFIED_AT_CHECK, name="ck_payments_verified_at_consistency"),
        CheckConstraint(
            "method NOT IN ('yape', 'plin', 'transferencia') OR reference IS NOT NULL",
            name="ck_payments_digital_reference",
        ),
        UniqueConstraint("organization_id", "id", name="uq_payments_organization_id"),
        Index(
            "uq_payments_org_method_reference",
            "organization_id",
            "method",
            "reference",
            unique=True,
            postgresql_where=text("reference IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["organization_id", "charge_id"],
            ["charges.organization_id", "charges.id"],
            ondelete="RESTRICT",
            name="fk_payments_organization_charge",
        ),
    )


class ChargeFollowUp(Base):
    """One open or closed collection case attached to a charge.

    The promised date is an operator-entered clinic-local calendar date. It is
    never derived from a payment instant or from charge aging, and the
    settlement path closes an open row atomically with the final payment.
    """

    __tablename__ = "charge_follow_ups"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_charge_follow_ups_organization",
        ),
        nullable=False,
    )
    charge_id: Mapped[int] = mapped_column(nullable=False)
    next_follow_up_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    state: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)

    __table_args__ = (
        CheckConstraint(FOLLOW_UP_STATE_CHECK, name="ck_charge_follow_ups_state"),
        CheckConstraint(
            FOLLOW_UP_CLOSE_REASON_CHECK,
            name="ck_charge_follow_ups_close_reason",
        ),
        CheckConstraint(FOLLOW_UP_CLOSURE_CHECK, name="ck_charge_follow_ups_closure"),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_charge_follow_ups_organization_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "charge_id"],
            ["charges.organization_id", "charges.id"],
            ondelete="RESTRICT",
            name="fk_charge_follow_ups_organization_charge",
        ),
        Index(
            "uq_charge_follow_ups_org_charge_open",
            "organization_id",
            "charge_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
        Index(
            "ix_charge_follow_ups_org_due",
            "organization_id",
            "next_follow_up_on",
            postgresql_where=text("state = 'open'"),
        ),
    )
