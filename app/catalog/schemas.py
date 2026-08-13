from pydantic import BaseModel, ConfigDict, Field


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    duration_minutes: int = Field(gt=0)
    is_active: bool = True


class ServiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    duration_minutes: int
    is_active: bool
