from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    unit: str = Field(min_length=1, max_length=20)
    kind: Literal["consumible", "reventa"]


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    unit: str
    kind: str
    is_active: bool


class ServiceConsumptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class ServiceConsumptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    service_execution_id: int
    product_id: int
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    consumed_at: datetime


class ChargeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional: defaults to the execution's own price snapshot (the charged
    # amount is never re-guessed from the catalog).
    amount: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _amount_positive(self) -> "ChargeCreate":
        if self.amount is not None and self.amount <= 0:
            raise ValueError("amount must be positive.")
        return self


class ChargeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    service_execution_id: int
    amount: Decimal
    paid: Decimal
    outstanding: Decimal
    created_at: datetime
    visit_id: int
    patient_id: int
    patient_name: str
    service_id: int
    service_name: str
    location_id: int
    location_name: str
    practitioner_id: int
    practitioner_name: str
    executed_at: datetime


PaymentMethod = Literal[
    "efectivo",
    "tarjeta",
    "yape",
    "plin",
    "transferencia",
    "link_pago",
]

DIGITAL_PAYMENT_METHODS = ("yape", "plin", "transferencia")


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    method: PaymentMethod
    reference: str | None = Field(default=None, min_length=1, max_length=60)
    receiver: str | None = Field(default=None, min_length=1, max_length=120)
    reconciliation_note: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _strip_reference(cls, values):
        if isinstance(values, dict) and isinstance(values.get("reference"), str):
            values = dict(values)
            values["reference"] = values["reference"].strip()
        return values

    @model_validator(mode="after")
    def _digital_requires_reference(self) -> "PaymentCreate":
        if self.method in DIGITAL_PAYMENT_METHODS and not self.reference:
            raise ValueError("reference is required for yape, plin and transferencia.")
        return self


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    charge_id: int
    amount: Decimal
    method: PaymentMethod
    paid_at: datetime
    reference: str | None
    receiver: str | None
    reconciliation_note: str | None
    verification_status: Literal["unverified", "verified"]
    verified_at: datetime | None


class PaymentVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconciliation_note: str | None = Field(default=None, min_length=1, max_length=500)


class ChargeFollowUpCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_follow_up_on: date
    note: str | None = Field(default=None, min_length=1, max_length=500)


class ChargeFollowUpReschedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_follow_up_on: date
    note: str | None = Field(default=None, min_length=1, max_length=500)


class ChargeFollowUpClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, min_length=1, max_length=500)


class ChargeFollowUpRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    charge_id: int
    next_follow_up_on: date
    note: str | None
    state: Literal["open", "closed"]
    opened_at: datetime
    closed_at: datetime | None
    close_reason: Literal["settled", "closed_by_operator"] | None
    charge_amount: Decimal
    charge_paid: Decimal
    charge_outstanding: Decimal
    is_active_case: bool
    patient_id: int
    patient_name: str
    service_id: int
    service_name: str
    location_id: int
    location_name: str
