from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MediaReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_media_id: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=100)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)


class InboundMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    provider: Literal["whatsapp"]
    channel_account_external_id: str = Field(min_length=1, max_length=128)
    provider_message_id: str = Field(min_length=1, max_length=255)
    external_contact_id: str = Field(min_length=1, max_length=255)
    phone_e164: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    message_type: Literal["text", "audio", "image"]
    text: str | None = Field(default=None, max_length=16000)
    media: MediaReference | None = None
    occurred_at: datetime

    @model_validator(mode="after")
    def _content_matches_type(self) -> "InboundMessageCreate":
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware.")
        if self.message_type == "text" and (not self.text or self.media is not None):
            raise ValueError("text messages require text and cannot include media.")
        if self.message_type in {"audio", "image"} and self.media is None:
            raise ValueError("audio and image messages require a media reference.")
        return self


class InboundReceipt(BaseModel):
    message_id: int
    conversation_id: int
    contact_identity_id: int
    duplicate: bool


class OutboundMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4096)


class OutboundReceipt(BaseModel):
    outbound_id: int
    message_id: int
    conversation_id: int
    status: str
    duplicate: bool


class OutboundClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=50)


class OutboundDispatchItem(BaseModel):
    outbound_id: int
    conversation_id: int
    idempotency_key: UUID
    payload: dict
    attempt_count: int


class OutboundResultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["sent", "delivered", "transient_failure", "permanent_failure"]
    provider_message_id: str | None = Field(default=None, max_length=255)
    error_code: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _result_shape(self) -> "OutboundResultCreate":
        if self.outcome in {"sent", "delivered"} and not self.provider_message_id:
            raise ValueError("successful delivery results require provider_message_id.")
        if self.outcome.endswith("failure") and not self.error_code:
            raise ValueError("failure results require error_code.")
        return self


class OutboundStatusRead(BaseModel):
    outbound_id: int
    status: str
    attempt_count: int
    next_attempt_at: datetime

