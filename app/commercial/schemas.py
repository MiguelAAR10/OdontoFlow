from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LeadCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    contact_phone: str | None = None
    contact_email: str | None = None
    acquisition_source: Literal["promotion", "referral", "direct"]
    service_need_id: int | None = None


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    contact_phone: str | None
    contact_email: str | None
    acquisition_source: str
    service_need_id: int | None
    commercial_status: str
