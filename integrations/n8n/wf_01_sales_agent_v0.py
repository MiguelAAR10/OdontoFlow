"""Deterministic, repository-backed harness for the WF-01 n8n contract.

The JSON export is the n8n artifact.  This module exists because the current
worker environment has no n8n runtime: it models only the workflow's
normalization, bounded debounce, authenticated HTTP calls, and test-provider
result so those boundaries can be exercised against real FastAPI/PostgreSQL
surfaces.  It is deliberately not an application service and has no database
or ORM imports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

import httpx


WORKFLOW_KEY = "WF-01"
WORKFLOW_VERSION = "0.1.0"
WORKFLOW_EXPORT = Path(__file__).with_name("workflows") / "WF-01-sales-agent-v0.json"
DEFAULT_CHANNEL_ACCOUNT = "odonto-smart-lab"
DEFAULT_DEBOUNCE_WINDOW = timedelta(milliseconds=250)
MAX_BUFFERED_EVENTS = 8
_PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")


class WorkflowContractError(ValueError):
    """The repository-backed workflow contract cannot be satisfied."""


class WorkflowHTTPError(RuntimeError):
    """A safe transport or non-success HTTP response from a workflow call."""

    def __init__(self, path: str, status_code: int | None = None) -> None:
        self.path = path
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"WF-01 HTTP call failed: {path}{status}")


class SyncHTTPClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


def _parse_occurred_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowContractError("occurred_at is required.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowContractError("occurred_at must be an ISO-8601 instant.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowContractError("occurred_at must be timezone-aware.")
    return parsed.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class NormalizedInbound:
    """The small allowlisted payload that crosses the backend ingress edge."""

    provider_message_id: str
    external_contact_id: str
    phone_e164: str
    text: str
    occurred_at: datetime
    channel_account_external_id: str = DEFAULT_CHANNEL_ACCOUNT
    provider: str = "test"
    schema_version: str = "1.0"
    message_type: str = "text"
    request_id: str = ""
    inbound_idempotency_key: str = ""

    @property
    def conversation_key(self) -> tuple[str, str, str]:
        """Stable pre-ingress key; canonical conversation_id comes from the app."""
        return (
            self.provider,
            self.channel_account_external_id,
            self.external_contact_id,
        )

    def backend_payload(self) -> dict[str, str]:
        """Return exactly the backend's inbound schema fields, never provider extras."""
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "channel_account_external_id": self.channel_account_external_id,
            "provider_message_id": self.provider_message_id,
            "external_contact_id": self.external_contact_id,
            "phone_e164": self.phone_e164,
            "message_type": self.message_type,
            "text": self.text,
            "occurred_at": _iso_z(self.occurred_at),
        }


def normalize_inbound(source: Mapping[str, Any]) -> NormalizedInbound:
    """Normalize a synthetic provider event with a strict input allowlist."""
    if not isinstance(source, Mapping):
        raise WorkflowContractError("Inbound event must be an object.")
    nested = source.get("body")
    payload: Mapping[str, Any] = nested if isinstance(nested, Mapping) else source

    if payload.get("schema_version", "1.0") != "1.0":
        raise WorkflowContractError("WF-01 requires schema_version=1.0.")
    provider = payload.get("provider", "test")
    if provider != "test":
        raise WorkflowContractError("WF-01 accepts provider=test only.")
    provider_message_id = payload.get("provider_message_id", payload.get("message_id"))
    external_contact_id = payload.get("external_contact_id", payload.get("contact_id"))
    if not isinstance(provider_message_id, str) or not provider_message_id.strip():
        raise WorkflowContractError("provider_message_id is required.")
    if len(provider_message_id) > 255:
        raise WorkflowContractError("provider_message_id is too long.")
    if not isinstance(external_contact_id, str) or not external_contact_id.strip():
        raise WorkflowContractError("external_contact_id is required.")
    if len(external_contact_id) > 255:
        raise WorkflowContractError("external_contact_id is too long.")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise WorkflowContractError("text is required.")
    text = text.strip()
    if len(text) > 16_000:
        raise WorkflowContractError("text is too long.")

    phone = payload.get("phone_e164", "+51999000001")
    if not isinstance(phone, str) or not _PHONE_PATTERN.fullmatch(phone):
        raise WorkflowContractError("phone_e164 is invalid.")
    channel = payload.get("channel_account_external_id", DEFAULT_CHANNEL_ACCOUNT)
    if not isinstance(channel, str) or not channel.strip() or len(channel) > 128:
        raise WorkflowContractError("channel_account_external_id is invalid.")

    return NormalizedInbound(
        provider_message_id=provider_message_id.strip(),
        external_contact_id=external_contact_id.strip(),
        phone_e164=phone,
        text=text,
        occurred_at=_parse_occurred_at(payload.get("occurred_at")),
        channel_account_external_id=channel.strip(),
        request_id=str(uuid4()),
        inbound_idempotency_key=str(uuid4()),
    )


@dataclass
class _PendingConversation:
    events: list[NormalizedInbound]
    due_at: datetime


class ConversationScopedDebounce:
    """Bounded process-local debounce storage used by the W4 POC.

    The key is provider/channel/contact because the canonical conversation ID
    is returned only after ingress.  Events are never silently discarded: a
    full buffer fails closed so transport retry and backend dedupe can recover.
    """

    def __init__(
        self,
        *,
        window: timedelta = DEFAULT_DEBOUNCE_WINDOW,
        max_events: int = MAX_BUFFERED_EVENTS,
    ) -> None:
        if window <= timedelta(0):
            raise ValueError("Debounce window must be positive.")
        if max_events < 1:
            raise ValueError("max_events must be positive.")
        self.window = window
        self.max_events = max_events
        self._pending: dict[tuple[str, str, str], _PendingConversation] = {}

    def add(self, event: NormalizedInbound, *, now: datetime | None = None) -> None:
        raw_time = now or datetime.now(UTC)
        if raw_time.tzinfo is None or raw_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        current_time = raw_time.astimezone(UTC)
        pending = self._pending.get(event.conversation_key)
        if pending is None:
            pending = _PendingConversation(events=[], due_at=current_time + self.window)
            self._pending[event.conversation_key] = pending
        if len(pending.events) >= self.max_events:
            raise WorkflowContractError("WF-01 debounce buffer is full.")
        pending.events.append(event)
        pending.due_at = current_time + self.window

    def flush(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        key: tuple[str, str, str] | None = None,
    ) -> tuple[NormalizedInbound, ...]:
        raw_time = now or datetime.now(UTC)
        if raw_time.tzinfo is None or raw_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware.")
        current_time = raw_time.astimezone(UTC)
        result: list[NormalizedInbound] = []
        keys = (key,) if key is not None else tuple(self._pending)
        for pending_key in keys:
            pending = self._pending.get(pending_key)
            if pending is None:
                continue
            if not force and pending.due_at > current_time:
                continue
            del self._pending[pending_key]
            result.extend(pending.events)
        return tuple(result)

    @property
    def pending_count(self) -> int:
        return sum(len(pending.events) for pending in self._pending.values())


@dataclass(frozen=True)
class WorkflowExecutionResult:
    """Observable result of one synthetic workflow execution."""

    normalized_events: tuple[NormalizedInbound, ...]
    inbound_receipts: tuple[dict[str, Any], ...]
    conversation_id: int
    latest_inbound_message_id: int
    duplicate: bool
    agent_response: dict[str, Any] | None
    outbound_receipt: dict[str, Any] | None
    test_provider_result: dict[str, Any] | None


def load_workflow_export() -> dict[str, Any]:
    """Load the checked-in export without contacting an n8n instance."""
    return json.loads(WORKFLOW_EXPORT.read_text(encoding="utf-8"))


def _post_json(
    client: SyncHTTPClient,
    path: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Bound transport retry; callers reuse mutation identity headers."""
    last_error: WorkflowHTTPError | None = None
    for attempt in range(max_attempts):
        try:
            response = client.post(path, headers=dict(headers), json=dict(payload))
        except httpx.HTTPError as exc:
            last_error = WorkflowHTTPError(path)
            if attempt + 1 < max_attempts:
                continue
            raise last_error from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            last_error = WorkflowHTTPError(path, response.status_code)
            if attempt + 1 < max_attempts:
                continue
            raise last_error
        if response.status_code >= 400:
            raise WorkflowHTTPError(path, response.status_code)
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise WorkflowContractError(f"WF-01 returned non-JSON from {path}.") from exc
        if not isinstance(body, dict):
            raise WorkflowContractError(f"WF-01 returned a non-object from {path}.")
        return body
    raise last_error or WorkflowHTTPError(path)


class WF01Runner:
    """Run WF-01 against injected HTTP clients (normally FastAPI TestClients)."""

    def __init__(
        self,
        *,
        backend_client: SyncHTTPClient,
        sales_agent_client: SyncHTTPClient,
        inbound_token: str,
        agent_token: str,
        debounce: ConversationScopedDebounce | None = None,
    ) -> None:
        if not inbound_token.strip() or not agent_token.strip():
            raise ValueError("WF-01 credentials are required.")
        self.backend_client = backend_client
        self.sales_agent_client = sales_agent_client
        self.inbound_token = inbound_token
        self.agent_token = agent_token
        self.debounce = debounce or ConversationScopedDebounce()

    def execute(
        self,
        source: Mapping[str, Any] | NormalizedInbound,
        *,
        flush: bool = True,
    ) -> WorkflowExecutionResult | None:
        event = source if isinstance(source, NormalizedInbound) else normalize_inbound(source)
        self.debounce.add(event, now=event.occurred_at)
        ready = self.debounce.flush(
            now=event.occurred_at + self.debounce.window,
            force=flush,
            key=event.conversation_key,
        )
        if not ready:
            return None

        inbound_receipts: list[dict[str, Any]] = []
        latest_new: dict[str, Any] | None = None
        for buffered in ready:
            receipt = _post_json(
                self.backend_client,
                "/internal/messages/inbound",
                buffered.backend_payload(),
                headers={
                    "Authorization": f"Bearer {self.inbound_token}",
                    "Idempotency-Key": buffered.inbound_idempotency_key,
                    "X-Request-Id": buffered.request_id,
                    "X-Correlation-Id": buffered.request_id,
                },
            )
            if not {"message_id", "conversation_id", "duplicate"}.issubset(receipt):
                raise WorkflowContractError("Inbound receipt is missing required fields.")
            inbound_receipts.append(receipt)
            if not receipt["duplicate"]:
                latest_new = receipt

        latest = latest_new or inbound_receipts[-1]
        conversation_id = int(latest["conversation_id"])
        latest_message_id = int(latest["message_id"])
        if latest_new is None:
            return WorkflowExecutionResult(
                normalized_events=ready,
                inbound_receipts=tuple(inbound_receipts),
                conversation_id=conversation_id,
                latest_inbound_message_id=latest_message_id,
                duplicate=True,
                agent_response=None,
                outbound_receipt=None,
                test_provider_result={
                    "provider": "test",
                    "status": "deduplicated",
                    "conversation_id": conversation_id,
                    "message_id": latest_message_id,
                },
            )

        agent_payload = _post_json(
            self.sales_agent_client,
            "/sales-agent/turn",
            {
                "conversation_id": conversation_id,
                "latest_inbound_message_id": latest_message_id,
            },
            headers={
                "X-Request-Id": str(uuid4()),
                "X-Correlation-Id": str(uuid4()),
            },
            max_attempts=1,
        )
        required_agent = {
            "conversation_id",
            "latest_inbound_message_id",
            "reply",
            "outcome",
            "handoff",
        }
        if not required_agent.issubset(agent_payload):
            raise WorkflowContractError("Sales Agent response is missing required fields.")
        if (
            int(agent_payload["conversation_id"]) != conversation_id
            or int(agent_payload["latest_inbound_message_id"]) != latest_message_id
            or not isinstance(agent_payload["reply"], str)
            or not agent_payload["reply"].strip()
        ):
            raise WorkflowContractError("Sales Agent response identifiers or reply are invalid.")

        outbound_key = str(uuid4())
        outbound = _post_json(
            self.backend_client,
            f"/internal/conversations/{conversation_id}/outbound",
            {"text": str(agent_payload["reply"])},
            headers={
                "Authorization": f"Bearer {self.agent_token}",
                "Idempotency-Key": outbound_key,
            },
        )
        required_outbound = {
            "outbound_id",
            "message_id",
            "conversation_id",
            "status",
            "duplicate",
        }
        if not required_outbound.issubset(outbound):
            raise WorkflowContractError("Outbound receipt is missing required fields.")
        if int(outbound["conversation_id"]) != conversation_id:
            raise WorkflowContractError("Outbound receipt belongs to another conversation.")

        provider_result = {
            "provider": "test",
            "status": "accepted",
            "delivery_mode": "synthetic_only",
            "outbound_id": int(outbound["outbound_id"]),
            "canonical_status": outbound["status"],
            "test_result_id": f"test-provider-{outbound['outbound_id']}",
        }
        return WorkflowExecutionResult(
            normalized_events=ready,
            inbound_receipts=tuple(inbound_receipts),
            conversation_id=conversation_id,
            latest_inbound_message_id=latest_message_id,
            duplicate=False,
            agent_response=agent_payload,
            outbound_receipt=outbound,
            test_provider_result=provider_result,
        )


__all__ = [
    "ConversationScopedDebounce",
    "DEFAULT_CHANNEL_ACCOUNT",
    "MAX_BUFFERED_EVENTS",
    "NormalizedInbound",
    "WF01Runner",
    "WORKFLOW_EXPORT",
    "WORKFLOW_KEY",
    "WORKFLOW_VERSION",
    "WorkflowContractError",
    "WorkflowExecutionResult",
    "load_workflow_export",
    "normalize_inbound",
]
