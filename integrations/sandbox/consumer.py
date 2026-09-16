"""Authorized one-shot consumer for the development-only sandbox provider.

The consumer has no provider fallback: it asks the authenticated outbound
boundary for ``provider=sandbox`` rows, posts only to the fixed local receiver
contract, and settles through the existing authenticated result endpoint.
Receiver failures become the queue's existing transient/permanent outcomes so
the durable claim lease remains the source of retry truth.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit
from uuid import UUID, uuid4

import httpx

from app.messaging.schemas import (
    OutboundDispatchItem,
    OutboundStatusRead,
    SandboxDeliveryReceiptRead,
    SandboxOutboundPayload,
)

SANDBOX_RECEIVER_PATH = "/internal/sandbox/receive"
DEFAULT_BACKEND_BASE_URL = "http://127.0.0.1:8000"
SANDBOX_PROVIDER = "sandbox"
SANDBOX_RECEIVER_UNAVAILABLE = "SANDBOX_RECEIVER_UNAVAILABLE"
SANDBOX_RECEIVER_REJECTED = "SANDBOX_RECEIVER_REJECTED"
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


class SyncHTTPClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


class SandboxConsumerError(RuntimeError):
    """The local sandbox consumer could not complete a safe dispatch."""


class SandboxConfigurationError(SandboxConsumerError):
    """Required local receiver or server credential configuration is missing."""


class SandboxBackendError(SandboxConsumerError):
    """The authenticated backend claim or settlement boundary failed."""


class SandboxReceiverUnavailable(SandboxConsumerError):
    """The explicitly configured receiver was unavailable or retryable."""


class SandboxReceiverRejected(SandboxConsumerError):
    """The receiver rejected the exact sandbox delivery contract."""


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _parse_local_origin(
    value: str,
    *,
    setting_name: str,
    allowed_paths: frozenset[str] = frozenset({"", "/"}),
) -> SplitResult:
    if not isinstance(value, str) or not value.strip():
        raise SandboxConfigurationError(f"{setting_name} is required.")
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SandboxConfigurationError(f"{setting_name} is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not _is_loopback(hostname)
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in allowed_paths
    ):
        raise SandboxConfigurationError(
            f"{setting_name} must be an explicit loopback origin without credentials."
        )
    return parsed


def _parse_receiver_url(value: str) -> SplitResult:
    return _parse_local_origin(
        value,
        setting_name="SANDBOX_RECEIVER_URL",
        allowed_paths=frozenset({SANDBOX_RECEIVER_PATH}),
    )


@dataclass(frozen=True)
class SandboxSettings:
    """Explicit local endpoints and the server-issued dispatcher credential."""

    backend_base_url: str
    receiver_url: str
    credential: str
    request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        _parse_local_origin(self.backend_base_url, setting_name="SANDBOX_BACKEND_URL")
        _parse_receiver_url(self.receiver_url)
        if not isinstance(self.credential, str) or not self.credential.strip():
            raise SandboxConfigurationError("SANDBOX_DISPATCHER_TOKEN is required.")
        if (
            not isinstance(self.request_timeout_seconds, (int, float))
            or isinstance(self.request_timeout_seconds, bool)
            or not math.isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds <= 0
        ):
            raise SandboxConfigurationError("Sandbox request timeout must be positive.")

    @classmethod
    def from_env(cls) -> "SandboxSettings":
        raw_timeout = os.environ.get("SANDBOX_REQUEST_TIMEOUT_SECONDS", "10.0")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise SandboxConfigurationError(
                "SANDBOX_REQUEST_TIMEOUT_SECONDS is invalid."
            ) from exc
        return cls(
            backend_base_url=os.environ.get("SANDBOX_BACKEND_URL", DEFAULT_BACKEND_BASE_URL),
            receiver_url=os.environ.get("SANDBOX_RECEIVER_URL", ""),
            credential=os.environ.get("SANDBOX_DISPATCHER_TOKEN", ""),
            request_timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class SandboxRunResult:
    claimed: int
    delivered: int
    failed: int
    statuses: tuple[OutboundStatusRead, ...]


class SandboxConsumer:
    """Poll, deliver and settle sandbox rows once through authenticated HTTP."""

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        http_client: SyncHTTPClient | None = None,
        receiver_client: SyncHTTPClient | None = None,
    ) -> None:
        self.settings = settings
        self._backend_client = http_client or httpx.Client(
            base_url=settings.backend_base_url,
            timeout=settings.request_timeout_seconds,
        )
        self._receiver_client = receiver_client or self._backend_client
        self._owns_backend_client = http_client is None

    def __enter__(self) -> "SandboxConsumer":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_backend_client:
            self._backend_client.close()  # type: ignore[attr-defined]

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        request_id = str(uuid4())
        return {
            "Authorization": f"Bearer {self.settings.credential}",
            "Idempotency-Key": idempotency_key or str(uuid4()),
            "X-Request-Id": request_id,
            "X-Correlation-Id": request_id,
        }

    @staticmethod
    def _settlement_idempotency_key(item: OutboundDispatchItem) -> str:
        """Derive one UUIDv4-shaped key for this durable delivery attempt."""
        digest = bytearray(
            hashlib.sha256(
                f"sandbox-settlement:{item.outbound_id}:{item.attempt_count}".encode(
                    "ascii"
                )
            ).digest()[:16]
        )
        digest[6] = (digest[6] & 0x0F) | 0x40
        digest[8] = (digest[8] & 0x3F) | 0x80
        return str(UUID(bytes=bytes(digest)))

    @staticmethod
    def _json_response(response: httpx.Response, *, operation: str) -> Any:
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise SandboxBackendError(f"Sandbox {operation} returned invalid JSON.") from exc

    def _post_backend(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        try:
            response = self._backend_client.post(
                path,
                headers=self._headers(idempotency_key=idempotency_key),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise SandboxBackendError(f"Sandbox backend call failed: {path}.") from exc
        if response.status_code >= 400:
            raise SandboxBackendError(
                f"Sandbox backend call failed: {path} (HTTP {response.status_code})."
            )
        return self._json_response(response, operation=path)

    def _post_receiver(self, payload: dict[str, Any]) -> Any:
        try:
            response = self._receiver_client.post(
                self.settings.receiver_url,
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise SandboxReceiverUnavailable("The sandbox receiver was unavailable.") from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise SandboxReceiverUnavailable("The sandbox receiver was unavailable.")
        if response.status_code >= 400:
            raise SandboxReceiverRejected("The sandbox receiver rejected the delivery.")
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise SandboxReceiverRejected("The sandbox receiver returned invalid JSON.") from exc

    def _claim(self, limit: int) -> tuple[OutboundDispatchItem, ...]:
        body = self._post_backend(
            "/internal/outbound/claim",
            {"limit": limit, "provider": SANDBOX_PROVIDER},
        )
        if not isinstance(body, list):
            raise SandboxBackendError("Sandbox claim returned an invalid collection.")
        try:
            return tuple(OutboundDispatchItem.model_validate(item) for item in body)
        except ValueError as exc:
            raise SandboxBackendError("Sandbox claim returned an invalid item.") from exc

    def _receive(
        self, item: OutboundDispatchItem, payload: SandboxOutboundPayload
    ) -> SandboxDeliveryReceiptRead:
        body = self._post_receiver(
            {
                "outbound_id": item.outbound_id,
                "payload": payload.model_dump(mode="json"),
            }
        )
        if not isinstance(body, dict):
            raise SandboxReceiverRejected("The sandbox receiver returned an invalid receipt.")
        try:
            receipt = SandboxDeliveryReceiptRead.model_validate(body)
        except ValueError as exc:
            raise SandboxReceiverRejected("The sandbox receiver returned an invalid receipt.") from exc
        expected_provider_id = f"sandbox-{item.outbound_id}"
        if (
            receipt.outbound_id != item.outbound_id
            or receipt.provider_message_id != expected_provider_id
        ):
            raise SandboxReceiverRejected("The sandbox receipt is not bound to the claimed row.")
        return receipt

    def _settle(
        self,
        item: OutboundDispatchItem,
        *,
        outcome: str,
        provider_message_id: str | None = None,
        error_code: str | None = None,
    ) -> OutboundStatusRead:
        payload: dict[str, Any] = {"outcome": outcome}
        if provider_message_id is not None:
            payload["provider_message_id"] = provider_message_id
        if error_code is not None:
            payload["error_code"] = error_code
        body = self._post_backend(
            f"/internal/outbound/{item.outbound_id}/result",
            payload,
            idempotency_key=self._settlement_idempotency_key(item),
        )
        if not isinstance(body, dict):
            raise SandboxBackendError("Sandbox settlement returned an invalid result.")
        try:
            return OutboundStatusRead.model_validate(body)
        except ValueError as exc:
            raise SandboxBackendError("Sandbox settlement returned an invalid result.") from exc

    def dispatch_once(self, *, limit: int = 10) -> SandboxRunResult:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise SandboxConfigurationError("Sandbox dispatch limit must be between 1 and 50.")
        items = self._claim(limit)
        statuses: list[OutboundStatusRead] = []
        delivered = 0
        failed = 0
        for item in items:
            try:
                payload = SandboxOutboundPayload.model_validate(item.payload)
                receipt = self._receive(item, payload)
            except SandboxReceiverUnavailable:
                status = self._settle(
                    item,
                    outcome="transient_failure",
                    error_code=SANDBOX_RECEIVER_UNAVAILABLE,
                )
                failed += 1
            except (SandboxReceiverRejected, ValueError):
                status = self._settle(
                    item,
                    outcome="permanent_failure",
                    error_code=SANDBOX_RECEIVER_REJECTED,
                )
                failed += 1
            else:
                status = self._settle(
                    item,
                    outcome="delivered",
                    provider_message_id=receipt.provider_message_id,
                )
                delivered += 1
            statuses.append(status)
        return SandboxRunResult(
            claimed=len(items),
            delivered=delivered,
            failed=failed,
            statuses=tuple(statuses),
        )
