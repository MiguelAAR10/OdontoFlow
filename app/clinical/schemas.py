from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class PatientCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    dni: str | None = Field(default=None, pattern=r"^\d{8}$")
    sexo: Literal["M", "F", "O"] | None = None
    phone: str | None = Field(default=None, max_length=25)
    birth_date: date | None = None


class PatientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    dni: str | None
    sexo: str | None
    phone: str | None
    birth_date: date | None


class VisitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: int
    # Appointment-origin rule: when given, the appointment must be confirmed
    # and its practitioner/location are used; otherwise practitioner_id and
    # location_id are required (walk-in attendance). The two modes are
    # mutually exclusive — a payload mixing them is rejected up front.
    appointment_id: int | None = None
    practitioner_id: int | None = None
    location_id: int | None = None

    @model_validator(mode="after")
    def _exclusive_origin(self) -> "VisitCreate":
        if self.appointment_id is not None and (
            self.practitioner_id is not None or self.location_id is not None
        ):
            raise ValueError(
                "appointment_id is mutually exclusive with practitioner_id/location_id."
            )
        if self.appointment_id is None and (
            self.practitioner_id is None or self.location_id is None
        ):
            raise ValueError(
                "practitioner_id and location_id are required when no appointment is given."
            )
        return self


class VisitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    patient_name: str
    appointment_id: int | None
    practitioner_id: int
    practitioner_name: str
    location_id: int
    location_name: str
    started_at: datetime


class ServiceExecutionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: int
    executed_price: Decimal = Field(ge=0)


class ServiceExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    visit_id: int
    service_id: int
    service_name: str
    executed_price: Decimal
    executed_at: datetime


class VisitDetailRead(VisitRead):
    executions: list[ServiceExecutionRead]
