"""Authenticated HTTP client for the canonical agent-tool gateway.

This module deliberately contains no SQLAlchemy session or canonical model
access. The Sales Agent can only observe or mutate business state through the
typed ``POST /agent-tools/call`` contract.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic_core import to_jsonable_python

from sales_agent.schemas import (
    AgentToolRequest,
    AgentToolResult,
    GatewayContractError,
    GatewayError,
    InboundMessage,
    MUTATION_TOOL_NAMES,
    OUTCOME_UNKNOWN,
    V0_TOOL_NAMES,
)


#: Failures where the request provably never reached the backend.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
#: Proxy statuses that say nothing about whether the backend committed.
_AMBIGUOUS_PROXY_STATUSES = frozenset({502, 504})


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def stable_idempotency_key(
    *,
    conversation_id: int,
    latest_inbound_message_id: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> UUID:
    """One key per logical action: same turn + tool + canonical args => same key.

    Datetimes are normalized to UTC, like the backend fingerprint, so the same
    instant with another offset replays instead of becoming a new command.
    """
    canonical_args = json.dumps(
        to_jsonable_python(_canonical(arguments)),
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(
        f"{conversation_id}:{latest_inbound_message_id}:{tool_name}:{canonical_args}".encode()
    ).digest()
    return UUID(bytes=digest[:16], version=4)


class BackendGateway:
    """Call the canonical backend with one configured sales-agent credential."""

    def __init__(
        self,
        base_url: str,
        credential: str | None,
        *,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self._client = http_client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._owns_client = http_client is None
        #: conversation_id -> latest inbound message id loaded for the turn;
        #: anchors the stable Idempotency-Key without threading it through
        #: every tool wrapper.
        self._turn_inbound: dict[int, int] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BackendGateway":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def call_tool(
        self,
        tool_name: str,
        *,
        conversation_id: int,
        arguments: dict[str, Any],
        latest_inbound_message_id: int | None = None,
    ) -> AgentToolResult:
        """Call one of the six allowlisted backend tools.

        A mutation carries a stable Idempotency-Key anchored on
        ``latest_inbound_message_id`` (explicit, or the one this gateway last
        loaded for the conversation), so a retry of the same logical action
        replays. With no anchor a fresh UUIDv4 is used. A mutation whose outcome
        cannot be known (sent but no answer) raises ``OUTCOME_UNKNOWN`` and is
        never retried here.
        """
        if tool_name not in V0_TOOL_NAMES:
            raise ValueError(f"Tool is not available in Sales Agent V0: {tool_name}")
        if self.credential is None or not self.credential.strip():
            raise GatewayError(
                "AUTHENTICATION_REQUIRED",
                "The Sales Agent backend credential is not configured.",
                status_code=503,
            )

        is_mutation = tool_name in MUTATION_TOOL_NAMES
        if latest_inbound_message_id is None:
            latest_inbound_message_id = self._turn_inbound.get(conversation_id)
        if not is_mutation:
            idempotency_key = None
        elif latest_inbound_message_id is None:
            idempotency_key = uuid4()
        else:
            idempotency_key = stable_idempotency_key(
                conversation_id=conversation_id,
                latest_inbound_message_id=latest_inbound_message_id,
                tool_name=tool_name,
                arguments=arguments,
            )
        envelope = AgentToolRequest(
            tool_version="1.1" if is_mutation else "1.0",
            tool_name=tool_name,
            conversation_id=conversation_id,
            request_id=uuid4(),
            correlation_id=uuid4(),
            idempotency_key=idempotency_key,
            arguments=arguments,
        )
        headers = {
            "Authorization": f"Bearer {self.credential}",
            "X-Request-Id": str(envelope.request_id),
            "X-Correlation-Id": str(envelope.correlation_id),
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = str(idempotency_key)

        try:
            response = self._client.post(
                "/agent-tools/call",
                headers=headers,
                json=envelope.model_dump(mode="json"),
            )
        except httpx.HTTPError as exc:
            if is_mutation and not isinstance(exc, _NOT_SENT_ERRORS):
                raise self._outcome_unknown() from exc
            raise GatewayError(
                "BACKEND_UNAVAILABLE",
                "The canonical backend is unavailable.",
                status_code=503,
            ) from exc

        if response.status_code >= 400:
            error = self._http_error(response)
            if (
                is_mutation
                and error.code == "BACKEND_REQUEST_FAILED"
                and response.status_code in _AMBIGUOUS_PROXY_STATUSES
            ):
                raise self._outcome_unknown()
            raise error
        try:
            return AgentToolResult.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise GatewayContractError(
                "INVALID_BACKEND_RESPONSE",
                "The canonical backend returned an invalid tool response.",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _outcome_unknown() -> GatewayError:
        return GatewayError(
            OUTCOME_UNKNOWN,
            "The backend outcome of this action is unknown; a human must verify it.",
            status_code=504,
        )

    @staticmethod
    def _http_error(response: httpx.Response) -> GatewayError:
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            details = error.get("details") if isinstance(error, dict) else None
        except (TypeError, ValueError):
            code = message = details = None
        return GatewayError(
            str(code or "BACKEND_REQUEST_FAILED"),
            str(message or "The canonical backend rejected the tool request."),
            status_code=response.status_code,
            details=details if isinstance(details, dict) else {},
        )

    def load_latest_inbound_message(
        self,
        conversation_id: int,
        message_id: int,
    ) -> InboundMessage:
        """Load one retained inbound message using the typed context tool."""
        result = self.call_tool(
            "get_reception_context",
            conversation_id=conversation_id,
            arguments={"as_of": date.today().isoformat()},
        )
        if result.status != "success" or result.data is None:
            error = result.error
            raise GatewayError(
                error.code if error is not None else "BACKEND_REQUEST_FAILED",
                error.message if error is not None else "The conversation context is unavailable.",
                details=error.details if error is not None else {},
            )
        context_conversation = result.data.get("conversation")
        if not isinstance(context_conversation, dict) or context_conversation.get("id") != conversation_id:
            raise GatewayContractError(
                "INVALID_BACKEND_RESPONSE",
                "The canonical backend returned the wrong conversation context.",
            )
        messages = result.data.get("recent_messages")
        if not isinstance(messages, list):
            raise GatewayContractError(
                "INVALID_BACKEND_RESPONSE",
                "The canonical backend returned no message context.",
            )
        for item in messages:
            if not isinstance(item, dict) or item.get("id") != message_id:
                continue
            try:
                inbound = InboundMessage.model_validate(item)
            except (TypeError, ValueError) as exc:
                raise GatewayContractError(
                    "INVALID_BACKEND_RESPONSE",
                    "The canonical backend returned an invalid inbound message.",
                ) from exc
            self._turn_inbound[conversation_id] = message_id
            return inbound
        raise GatewayError(
            "NOT_FOUND",
            "The requested inbound message is not available.",
            status_code=404,
        )


__all__ = [
    "AgentToolRequest",
    "AgentToolResult",
    "BackendGateway",
    "GatewayContractError",
    "GatewayError",
    "V0_TOOL_NAMES",
    "stable_idempotency_key",
]
