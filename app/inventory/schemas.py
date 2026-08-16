from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class EntryCreate(BaseModel):
    """A purchase/initial stock input, targeted at one location."""

    model_config = ConfigDict(extra="forbid")

    location_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)


class AdjustmentCreate(BaseModel):
    """A reason-required correction at one location (a movement row, always)."""

    model_config = ConfigDict(extra="forbid")

    location_id: int = Field(gt=0)
    quantity: Decimal
    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def _non_zero(self) -> "AdjustmentCreate":
        if self.quantity == 0:
            raise ValueError("quantity must be non-zero for an adjustment.")
        return self


class TransferCreate(BaseModel):
    """Move stock between two locations of the same organization."""

    model_config = ConfigDict(extra="forbid")

    origin_location_id: int = Field(gt=0)
    destination_location_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _distinct_locations(self) -> "TransferCreate":
        if self.origin_location_id == self.destination_location_id:
            raise ValueError("origin and destination locations must differ.")
        return self


class MovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    location_id: int
    type: str
    quantity: Decimal
    unit_price: Decimal | None
    reason: str | None
    id_consumo_origen: int | None
    transfer_id: str | None
    moved_at: datetime


class BalanceRead(BaseModel):
    product_id: int
    location_id: int
    available: Decimal


class TransferRead(BaseModel):
    transfer_id: str
    product_id: int
    origin_location_id: int
    destination_location_id: int
    quantity: Decimal
    reason: str | None
    out_movement_id: int
    in_movement_id: int
