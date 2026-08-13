from pydantic import BaseModel, ConfigDict, Field


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    timezone: str = Field(min_length=1, max_length=64)


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    timezone: str
    is_active: bool


class PractitionerCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=250)
    is_active: bool = True


class PractitionerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
    is_active: bool


class CapabilityCreate(BaseModel):
    practitioner_id: int
    service_id: int
    location_id: int
    is_active: bool = True


class CapabilityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    practitioner_id: int
    service_id: int
    location_id: int
    is_active: bool
