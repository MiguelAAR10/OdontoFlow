from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ToolName = Literal[
    "list_services",
    "list_locations",
    "list_eligible_practitioners",
    "query_available_slots",
    "get_appointment",
    "list_contact_appointments",
    "propose_appointment",
    "confirm_appointment",
    "get_reception_context",
    "get_contact_profile",
    "register_contact_profile",
    "cancel_appointment",
    "propose_reschedule",
    "confirm_reschedule",
    "request_human_handoff",
    "resume_automation",
]

READ_TOOL_NAMES = {
    "list_services",
    "list_locations",
    "list_eligible_practitioners",
    "query_available_slots",
    "get_appointment",
    "list_contact_appointments",
    "get_reception_context",
    "get_contact_profile",
}
MUTATION_TOOL_NAMES = {
    "propose_appointment",
    "confirm_appointment",
    "register_contact_profile",
    "cancel_appointment",
    "propose_reschedule",
    "confirm_reschedule",
    "request_human_handoff",
    "resume_automation",
}


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReceptionContextArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date = Field(default_factory=date.today)


class RegisterContactProfileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=255)
    dni: str | None = Field(default=None, pattern=r"^\d{8}$")
    birth_date: date | None = None


class CancelAppointmentArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: int = Field(ge=1)
    confirmation: Literal["CONFIRMO_CANCELACION"]
    reason: str | None = Field(default=None, max_length=300)


class ProposeRescheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: int = Field(ge=1)
    new_start: datetime

    @model_validator(mode="after")
    def validate_start(self):
        if self.new_start.utcoffset() is None:
            raise ValueError("Appointment start must include a UTC offset.")
        return self


class ConfirmRescheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int = Field(ge=1)
    confirmation_token: UUID


class HumanHandoffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: Literal[
        "requested_by_contact",
        "urgent_symptoms",
        "complaint",
        "pricing_exception",
        "clinical_case",
        "low_confidence",
        "other",
    ]
    reason_summary: str = Field(min_length=5, max_length=500)


class EligiblePractitionersArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: int = Field(ge=1)
    location_id: int = Field(ge=1)


class AvailableSlotsArguments(EligiblePractitionersArguments):
    window_start: datetime
    window_end: datetime

    @model_validator(mode="after")
    def validate_window(self):
        if self.window_start.utcoffset() is None or self.window_end.utcoffset() is None:
            raise ValueError("Availability window dates must include a UTC offset.")
        if self.window_end <= self.window_start:
            raise ValueError("Availability window end must be after its start.")
        return self


class AppointmentArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: int = Field(ge=1)


class ContactAppointmentsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_date: datetime | None = None
    to_date: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self):
        dates = (self.from_date, self.to_date)
        if any(value is not None and value.utcoffset() is None for value in dates):
            raise ValueError("Appointment filter dates must include a UTC offset.")
        if (
            self.from_date is not None
            and self.to_date is not None
            and self.to_date <= self.from_date
        ):
            raise ValueError("Appointment filter end must be after its start.")
        return self


class ProposeAppointmentArguments(EligiblePractitionersArguments):
    full_name: str = Field(min_length=2, max_length=255)
    practitioner_id: int = Field(ge=1)
    start: datetime

    @model_validator(mode="after")
    def validate_start(self):
        if self.start.utcoffset() is None:
            raise ValueError("Appointment start must include a UTC offset.")
        return self


class ConfirmAppointmentArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int = Field(ge=1)
    confirmation_token: UUID


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_version: Literal["1.0", "1.1"]
    tool_name: ToolName
    conversation_id: int = Field(ge=1)
    request_id: UUID
    correlation_id: UUID
    idempotency_key: UUID | None = Field(...)
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_version_and_idempotency(self):
        if self.tool_name in READ_TOOL_NAMES:
            if self.tool_version != "1.0" or self.idempotency_key is not None:
                raise ValueError(
                    "Read tools require version 1.0 and idempotency_key=null."
                )
            return self
        if self.tool_version != "1.1":
            raise ValueError("Booking tools require version 1.1.")
        if self.idempotency_key is None or self.idempotency_key.version != 4:
            raise ValueError("Booking tools require a UUIDv4 idempotency key.")
        return self


class AgentToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool
    details: dict[str, Any]


class AgentToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_version: Literal["1.0", "1.1"] = "1.0"
    status: Literal["success", "error"]
    data: dict[str, Any] | None
    error: AgentToolError | None
    request_id: str
    correlation_id: str
    duration_ms: int = Field(ge=0)

