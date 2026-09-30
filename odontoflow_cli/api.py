"""HTTP-only client for the OdontoFlow API, shared by the CLI and the MCP server.

No ``app`` import, no database driver, no printing: this module only speaks the
public HTTP contract with one bearer token. Authorization, the agent allowlist,
L4 refusal and human-only approval all stay server-side.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"


class ApiError(Exception):
    """Non-2xx response or envelope ``status=error``; ``body`` is the server's JSON."""

    def __init__(self, code: str, message: str, *, status: int, body: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.body = body


class TransportError(Exception):
    """The API could not be reached (connection refused, timeout)."""

    code = "TRANSPORT_ERROR"


@dataclass(frozen=True)
class Descriptor:
    name: str
    tool_version: str
    effect: str
    level: str
    needs_conversation: bool
    description: str
    arguments_schema: dict[str, Any]

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> "Descriptor":
        return cls(**{field: item[field] for field in cls.__dataclass_fields__})


def visible(descriptors: list[Descriptor]) -> list[Descriptor]:
    """Client-side policy 1 (defense in depth): never offer an L4 tool."""
    return [d for d in descriptors if d.level != "L4"]


def stable_idempotency_key(
    *, conversation_id: int | None, tool_name: str, arguments: dict[str, Any]
) -> str:
    """Same conversation + tool + canonical args => same UUIDv4 (B1 gateway rule)."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{conversation_id}:{tool_name}:{canonical}".encode()).digest()
    return str(UUID(bytes=digest[:16], version=4))


def _error_from(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"code": "HTTP_ERROR", "message": response.text[:200], "details": {}}}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code, message = error.get("code", "HTTP_ERROR"), error.get("message", "")
    else:  # e.g. FastAPI's 422 {"detail": [...]}
        code, message = "HTTP_ERROR", f"HTTP {response.status_code}"
    return ApiError(code, message, status=response.status_code, body=body)


class OdontoflowApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        http_client: httpx.Client | None = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._token = token
        self._client = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def __repr__(self) -> str:  # never leak the token through a repr
        return "OdontoflowApi(<token hidden>)"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        idempotency_key: str | None = None,
        trace: tuple[str, str] | None = None,
    ) -> Any:
        request_id, correlation_id = trace or (str(uuid4()), str(uuid4()))
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(method, path, headers=headers, json=json_body)
        except httpx.TransportError as exc:
            raise TransportError(f"{type(exc).__name__} reaching the OdontoFlow API") from None
        if response.status_code >= 300:
            raise _error_from(response)
        return response.json()

    # --- routes (no others) ----------------------------------------------------

    def catalog(self) -> list[Descriptor]:
        body = self.request("GET", "/agent-tools/catalog")
        return [Descriptor.from_json(item) for item in body["tools"]]

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        conversation_id: int | None,
        descriptor: Descriptor | None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST the envelope; returns the success envelope or raises ``ApiError``.

        A tool missing from the caller's catalog (``descriptor is None``) is still
        sent as a mutation envelope so the server, not the client, refuses it.
        """
        read = descriptor is not None and descriptor.effect == "read"
        version = descriptor.tool_version if descriptor is not None else "1.1"
        key = None if read else (idempotency_key or str(uuid4()))
        request_id, correlation_id = str(uuid4()), str(uuid4())
        envelope = {
            "tool_version": version,
            "tool_name": tool_name,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "idempotency_key": key,
            "arguments": arguments,
        }
        body = self.request(
            "POST", "/agent-tools/call", json_body=envelope, idempotency_key=key,
            trace=(request_id, correlation_id),
        )
        if body.get("status") != "success":
            error = body.get("error") or {}
            raise ApiError(error.get("code", "TOOL_ERROR"), error.get("message", ""),
                           status=200, body=body)
        return body

    def inbox(self) -> dict[str, Any]:
        return self.request("GET", "/agent/inbox")

    def approve(
        self, proposal_id: int, *, payload_hash: str, note: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"payload_hash": payload_hash}
        if note is not None:
            body["note"] = note
        return self.request("POST", f"/agent/proposals/{proposal_id}/approve", json_body=body,
                            idempotency_key=idempotency_key or str(uuid4()))

    def decline(self, proposal_id: int, *, note: str | None = None) -> dict[str, Any]:
        body = {"note": note} if note is not None else {}
        return self.request("POST", f"/agent/proposals/{proposal_id}/decline", json_body=body)

    def start_run(self, agent_key: str) -> dict[str, Any]:
        return self.request("POST", "/agent-runs", json_body={"agent_key": agent_key},
                            idempotency_key=str(uuid4()))

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/me")
