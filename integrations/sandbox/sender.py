"""Development-only sender for the authenticated sandbox inbound contract.

This adapter owns no conversation state and never accesses PostgreSQL. It
normalizes one explicitly sandbox-tagged event, persists it through canonical
ingress, invokes one authenticated Sales Agent turn, and persists the agent's
reply through the existing conversation outbound command. The committed
``SandboxConsumer`` remains the only outbound delivery path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

import httpx

from app.messaging.schemas import InboundReceipt, OutboundReceipt
from sales_agent.schemas import SalesAgentTurnResponse

SANDBOX_PROVIDER = "sandbox"
MAX_INBOUND_TEXT = 16_000
_PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")


class SyncHTTPClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


class SandboxSenderError(RuntimeError):
    """The controlled sandbox sender could not complete its HTTP sequence."""


class SandboxSenderConfigurationError(SandboxSenderError):
    """A required server-issued sender credential is missing."""


class SandboxSenderTransportError(SandboxSenderError):
    """An authenticated sandbox boundary was unavailable or rejected."""


class SandboxSenderContractError(SandboxSenderError):
    """A boundary returned a response outside its typed contract."""


def _parse_occurred_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SandboxSenderContractError("occurred_at is required.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SandboxSenderContractError(
            "occurred_at must be an ISO-8601 instant."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SandboxSenderContractError("occurred_at must be timezone-aware.")
    return parsed.astimezone(UTC)


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SandboxSenderContractError(f"{field} is required.")
    value = value.strip()
    if len(value) > maximum:
        raise SandboxSenderContractError(f"{field} is too long.")
    return value


@dataclass(frozen=True)
class SandboxInboundEvent:
    """The allowlisted event shape sent to canonical sandbox ingress."""

    provider_message_id: str
    external_contact_id: str
    phone_e164: str
    text: str
    occurred_at: datetime
    channel_account_external_id: str
    request_id: UUID
    inbound_idempotency_key: UUID

    def backend_payload(self) -> dict[str, str]:
        return {
            "schema_version": "1.0",
            "provider": SANDBOX_PROVIDER,
            "channel_account_external_id": self.channel_account_external_id,
            "provider_message_id": self.provider_message_id,
            "external_contact_id": self.external_contact_id,
            "phone_e164": self.phone_e164,
            "message_type": "text",
            "text": self.text,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
        }


def normalize_sandbox_inbound(source: Mapping[str, Any]) -> SandboxInboundEvent:
    """Normalize one explicit sandbox event without accepting provider fallback."""
    if not isinstance(source, Mapping):
        raise SandboxSenderContractError("Inbound event must be an object.")
    nested = source.get("body")
    payload: Mapping[str, Any] = nested if isinstance(nested, Mapping) else source

    if payload.get("schema_version") != "1.0":
        raise SandboxSenderContractError("Sandbox requires schema_version=1.0.")
    if payload.get("provider") != SANDBOX_PROVIDER:
        raise SandboxSenderContractError("Sandbox sender accepts provider=sandbox only.")

    provider_message_id = _bounded_text(
        payload.get("provider_message_id"),
        field="provider_message_id",
        maximum=255,
    )
    external_contact_id = _bounded_text(
        payload.get("external_contact_id"),
        field="external_contact_id",
        maximum=255,
    )
    phone_e164 = payload.get("phone_e164")
    if not isinstance(phone_e164, str) or not _PHONE_PATTERN.fullmatch(phone_e164):
        raise SandboxSenderContractError("phone_e164 is invalid.")
    text = _bounded_text(payload.get("text"), field="text", maximum=MAX_INBOUND_TEXT)
    channel = _bounded_text(
        payload.get("channel_account_external_id"),
        field="channel_account_external_id",
        maximum=128,
    )
    if payload.get("message_type") != "text":
        raise SandboxSenderContractError("Sandbox sender accepts text messages only.")

    return SandboxInboundEvent(
        provider_message_id=provider_message_id,
        external_contact_id=external_contact_id,
        phone_e164=phone_e164,
        text=text,
        occurred_at=_parse_occurred_at(payload.get("occurred_at")),
        channel_account_external_id=channel,
        request_id=uuid4(),
        inbound_idempotency_key=uuid4(),
    )


@dataclass(frozen=True)
class SandboxInboundResult:
    """Observable result of one sandbox ingress-to-outbound pass."""

    inbound_receipt: dict[str, Any]
    conversation_id: int
    message_id: int
    duplicate: bool
    agent_response: dict[str, Any] | None
    outbound_receipt: dict[str, Any] | None


class SandboxInboundSender:
    """Run one authenticated sandbox inbound event through existing boundaries."""

    def __init__(
        self,
        *,
        backend_client: SyncHTTPClient,
        sales_agent_client: SyncHTTPClient,
        inbound_token: str,
        agent_token: str,
    ) -> None:
        if not isinstance(inbound_token, str) or not inbound_token.strip():
            raise SandboxSenderConfigurationError("The sandbox inbound credential is required.")
        if not isinstance(agent_token, str) or not agent_token.strip():
            raise SandboxSenderConfigurationError("The sandbox agent credential is required.")
        self.backend_client = backend_client
        self.sales_agent_client = sales_agent_client
        self.inbound_token = inbound_token
        self.agent_token = agent_token

    @staticmethod
    def _headers(
        token: str,
        *,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, str]:
        request_id = str(uuid4())
        return {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key or str(uuid4()),
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id or request_id,
        }

    @staticmethod
    def _post_json(
        client: SyncHTTPClient,
        path: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        max_attempts: int,
    ) -> dict[str, Any]:
        last_status: int | None = None
        for attempt in range(max_attempts):
            try:
                response = client.post(path, headers=dict(headers), json=dict(payload))
            except httpx.HTTPError as exc:
                if attempt + 1 < max_attempts:
                    continue
                raise SandboxSenderTransportError(
                    f"Sandbox HTTP call failed: {path}."
                ) from exc
            last_status = response.status_code
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt + 1 < max_attempts:
                    continue
                raise SandboxSenderTransportError(
                    f"Sandbox HTTP call failed: {path} (HTTP {response.status_code})."
                )
            if response.status_code >= 400:
                raise SandboxSenderTransportError(
                    f"Sandbox HTTP call failed: {path} (HTTP {response.status_code})."
                )
            try:
                body = response.json()
            except (TypeError, ValueError) as exc:
                raise SandboxSenderContractError(
                    f"Sandbox HTTP call returned invalid JSON: {path}."
                ) from exc
            if not isinstance(body, dict):
                raise SandboxSenderContractError(
                    f"Sandbox HTTP call returned a non-object: {path}."
                )
            return body
        raise SandboxSenderTransportError(
            f"Sandbox HTTP call failed: {path} (HTTP {last_status})."
        )

    @staticmethod
    def _inbound_receipt(body: Mapping[str, Any]) -> InboundReceipt:
        try:
            return InboundReceipt.model_validate(body)
        except (TypeError, ValueError) as exc:
            raise SandboxSenderContractError(
                "Canonical inbound returned an invalid receipt."
            ) from exc

    @staticmethod
    def _agent_response(body: Mapping[str, Any]) -> SalesAgentTurnResponse:
        try:
            return SalesAgentTurnResponse.model_validate(body)
        except (TypeError, ValueError) as exc:
            raise SandboxSenderContractError(
                "Sales Agent returned an invalid response."
            ) from exc

    @staticmethod
    def _outbound_receipt(body: Mapping[str, Any]) -> OutboundReceipt:
        try:
            return OutboundReceipt.model_validate(body)
        except (TypeError, ValueError) as exc:
            raise SandboxSenderContractError(
                "Canonical outbound returned an invalid receipt."
            ) from exc

    def send(
        self, source: Mapping[str, Any] | SandboxInboundEvent
    ) -> SandboxInboundResult:
        """Persist, process, and persist one sandbox response.

        A canonical duplicate is an already-processed provider event. It is
        returned to the caller without invoking the Sales Agent or creating a
        second outbound intent, matching the existing WF-01 replay contract.
        """
        event = source if isinstance(source, SandboxInboundEvent) else normalize_sandbox_inbound(source)
        inbound_body = self._post_json(
            self.backend_client,
            "/internal/messages/inbound",
            event.backend_payload(),
            headers=self._headers(
                self.inbound_token,
                idempotency_key=str(event.inbound_idempotency_key),
                correlation_id=str(event.request_id),
            ),
            max_attempts=3,
        )
        inbound = self._inbound_receipt(inbound_body)
        if inbound.duplicate:
            return SandboxInboundResult(
                inbound_receipt=inbound.model_dump(mode="json"),
                conversation_id=inbound.conversation_id,
                message_id=inbound.message_id,
                duplicate=True,
                agent_response=None,
                outbound_receipt=None,
            )

        agent_body = self._post_json(
            self.sales_agent_client,
            "/sales-agent/turn",
            {
                "conversation_id": inbound.conversation_id,
                "latest_inbound_message_id": inbound.message_id,
            },
            headers=self._headers(
                self.agent_token,
                correlation_id=str(event.request_id),
            ),
            # A turn can call mutating tools; the existing W4 runner deliberately
            # does not transport-retry it.
            max_attempts=1,
        )
        agent = self._agent_response(agent_body)
        if (
            agent.conversation_id != inbound.conversation_id
            or agent.latest_inbound_message_id != inbound.message_id
        ):
            raise SandboxSenderContractError(
                "Sales Agent response is bound to the wrong inbound conversation."
            )

        outbound_body = self._post_json(
            self.backend_client,
            f"/internal/conversations/{inbound.conversation_id}/outbound",
            {"text": agent.reply},
            headers=self._headers(self.agent_token),
            # The outbound command is idempotent on this key, so retrying a
            # lost response cannot create a second logical outbound message.
            max_attempts=3,
        )
        outbound = self._outbound_receipt(outbound_body)
        if outbound.conversation_id != inbound.conversation_id:
            raise SandboxSenderContractError(
                "Canonical outbound receipt belongs to another conversation."
            )

        return SandboxInboundResult(
            inbound_receipt=inbound.model_dump(mode="json"),
            conversation_id=inbound.conversation_id,
            message_id=inbound.message_id,
            duplicate=False,
            agent_response=agent.model_dump(mode="json"),
            outbound_receipt=outbound.model_dump(mode="json"),
        )


__all__ = [
    "SandboxInboundEvent",
    "SandboxInboundResult",
    "SandboxInboundSender",
    "SandboxSenderConfigurationError",
    "SandboxSenderContractError",
    "SandboxSenderError",
    "SandboxSenderTransportError",
    "normalize_sandbox_inbound",
]
