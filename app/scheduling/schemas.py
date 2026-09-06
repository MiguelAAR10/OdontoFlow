from datetime import datetime, time

from pydantic import BaseModel, ConfigDict, Field


class AvailabilityRuleCreate(BaseModel):
    practitioner_id: int
    location_id: int
    day_of_week: int = Field(ge=0, le=6)
    start_local: time
    end_local: time


class AvailabilityRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    practitioner_id: int
    location_id: int
    day_of_week: int
    start_local: time
    end_local: time


class ScheduleBlockCreate(BaseModel):
    practitioner_id: int
    location_id: int
    start_utc: datetime
    end_utc: datetime


class ScheduleBlockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    practitioner_id: int
    location_id: int
    start_utc: datetime
    end_utc: datetime


class SlotQuery(BaseModel):
    service_id: int
    location_id: int
    window_start: datetime
    window_end: datetime


class SlotResult(BaseModel):
    practitioner_id: int
    start: datetime
    end: datetime


class AppointmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int
    service_id: int
    location_id: int
    practitioner_id: int
    start: datetime


class AppointmentCancel(BaseModel):
    """Empty by design: the appointment is identified by the path, and nothing
    about the cancellation is caller-supplied. ``extra='forbid'`` keeps a
    client from smuggling state through the body."""

    model_config = ConfigDict(extra="forbid")


class AppointmentReschedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_start: datetime


class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    service_id: int
    practitioner_id: int
    location_id: int
    start_utc: datetime
    end_utc: datetime
    state: str
    patient_id: int | None = None


class AppointmentListItem(AppointmentRead):
    """Agenda read DTO: the appointment plus the names the agenda renders.

    The names are joined at the application boundary (relationships), so the
    frontend never needs per-row lookups and never sees raw mockData shapes.
    """

    lead_name: str
    service_name: str
    practitioner_name: str
    location_name: str
    patient_name: str | None = None
