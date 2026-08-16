from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class EntryCreate(BaseModel):
    """A purchase/initial stock input (legacy SP adapted to HTTP)."""

    model_config = ConfigDict(extra="forbid")

    quantity: Decimal = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)


class AdjustmentCreate(BaseModel):
    """A reason-required correction (every stock change is a movement row)."""

    model_config = ConfigDict(extra="forbid")

    quantity: Decimal
    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def _non_zero(self) -> "AdjustmentCreate":
        if self.quantity == 0:
            raise ValueError("quantity must be non-zero for an adjustment.")
        return self


class MovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    type: str
    quantity: Decimal
    unit_price: Decimal | None
    reason: str | None
    id_consumo_origen: int | None
    moved_at: datetime


class BalanceRead(BaseModel):
    product_id: int
    available: Decimal
