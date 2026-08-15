from datetime import datetime
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


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    method: str = Field(min_length=1, max_length=50)


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    charge_id: int
    amount: Decimal
    method: str
    paid_at: datetime
