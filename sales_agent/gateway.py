"""Authenticated HTTP client for the canonical agent-tool gateway.

This module deliberately contains no SQLAlchemy session or canonical model
access. The Sales Agent can only observe or mutate business state through the
typed ``POST /agent-tools/call`` contract.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import httpx

from sales_agent.schemas import (
    AgentToolRequest,
    AgentToolResult,
    GatewayContractError,
    GatewayError,
    InboundMessage,
    MUTATION_TOOL_NAMES,
    V0_TOOL_NAMES,
)


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
    ) -> AgentToolResult:
        """Call one of the seven allowlisted backend tools."""
        if tool_name not in V0_TOOL_NAMES:
            raise ValueError(f"Tool is not available in Sales Agent V0: {tool_name}")
        if self.credential is None or not self.credential.strip():
            raise GatewayError(
                "AUTHENTICATION_REQUIRED",
                "The Sales Agent backend credential is not configured.",
                status_code=503,
            )

        is_mutation = tool_name in MUTATION_TOOL_NAMES
        idempotency_key = uuid4() if is_mutation else None
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
            raise GatewayError(
                "BACKEND_UNAVAILABLE",
                "The canonical backend is unavailable.",
                status_code=503,
            ) from exc

        if response.status_code >= 400:
            raise self._http_error(response)
        try:
            return AgentToolResult.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise GatewayContractError(
                "INVALID_BACKEND_RESPONSE",
                "The canonical backend returned an invalid tool response.",
                status_code=response.status_code,
            ) from exc

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
                return InboundMessage.model_validate(item)
            except (TypeError, ValueError) as exc:
                raise GatewayContractError(
                    "INVALID_BACKEND_RESPONSE",
                    "The canonical backend returned an invalid inbound message.",
                ) from exc
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
]
