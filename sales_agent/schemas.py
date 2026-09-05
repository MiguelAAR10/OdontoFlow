"""Typed contracts owned by the Sales Agent HTTP boundary."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentToolName = Literal[
    "get_reception_context",
    "list_services",
    "list_locations",
    "query_available_slots",
    "propose_appointment",
    "confirm_appointment",
    "request_human_handoff",
]

V0_TOOL_NAMES = frozenset(
    {
        "get_reception_context",
        "list_services",
        "list_locations",
        "query_available_slots",
        "propose_appointment",
        "confirm_appointment",
        "request_human_handoff",
    }
)
READ_TOOL_NAMES = frozenset(
    {"get_reception_context", "list_services", "list_locations", "query_available_slots"}
)
MUTATION_TOOL_NAMES = frozenset(
    {"propose_appointment", "confirm_appointment", "request_human_handoff"}
)


class AgentToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_version: Literal["1.0", "1.1"]
    tool_name: AgentToolName
    conversation_id: int = Field(ge=1)
    request_id: UUID
    correlation_id: UUID
    idempotency_key: UUID | None
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_transport(self) -> "AgentToolRequest":
        if self.tool_name in READ_TOOL_NAMES:
            if self.tool_version != "1.0" or self.idempotency_key is not None:
                raise ValueError("Read tools require version 1.0 and no idempotency key.")
        elif self.tool_version != "1.1" or self.idempotency_key is None:
            raise ValueError("Mutation tools require version 1.1 and a UUIDv4 key.")
        elif self.idempotency_key.version != 4:
            raise ValueError("Mutation tools require a UUIDv4 key.")
        return self


class AgentToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool
    details: dict[str, Any]


class AgentToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_version: Literal["1.0", "1.1"]
    status: Literal["success", "error"]
    data: dict[str, Any] | None
    error: AgentToolError | None
    request_id: str
    correlation_id: str
    duration_ms: int = Field(ge=0)


class InboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    text: str = Field(min_length=1)
    direction: Literal["inbound"] = "inbound"
    occurred_at: datetime | None = None


class SalesAgentTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: int = Field(ge=1)
    latest_inbound_message_id: int = Field(ge=1)


SalesAgentOutcome = Literal["continue", "proposed", "confirmed", "handoff"]


class SalesAgentResponse(BaseModel):
    """The only model-authored final shape returned from an agent turn."""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=4096)
    outcome: SalesAgentOutcome
    handoff: bool


class SalesAgentTurnResponse(SalesAgentResponse):
    model_config = ConfigDict(extra="forbid")

    conversation_id: int = Field(ge=1)
    latest_inbound_message_id: int = Field(ge=1)


class GatewayError(Exception):
    """A safe typed error from the authenticated backend gateway."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class GatewayContractError(GatewayError):
    """The backend returned a response that cannot satisfy the typed contract."""


class AgentUnavailableError(RuntimeError):
    """The optional runtime dependencies are not installed."""
