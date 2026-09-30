"""Build an MCP server whose tools are the caller's HTTP catalog, proxied.

The catalog is fetched once at startup with the process token. Each non-L4
descriptor becomes one MCP tool that POSTs ``/agent-tools/call``; three fixed
tools cover the inbox, the cobranza run and ``/me``. Approve/decline are never
exposed (client-side policy 2): approval is a typed human act.

stdout is the JSON-RPC channel on stdio: nothing here prints.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from odontoflow_cli.api import (
    ApiError,
    Descriptor,
    OdontoflowApi,
    TransportError,
    stable_idempotency_key,
    visible,
)


class StartupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _guard(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except ApiError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from None
    except TransportError as exc:
        raise ToolError(f"{exc.code}: {exc}") from None


def _proxy(api: OdontoflowApi, descriptor: Descriptor) -> Callable[..., dict[str, Any]]:
    """A fresh closure per descriptor (no loop late binding)."""

    def call(
        arguments: dict[str, Any] | None = None, conversation_id: int | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        key = None
        if descriptor.effect != "read":
            # Stable per logical action: an MCP/LLM retry replays, never re-executes.
            key = stable_idempotency_key(
                conversation_id=conversation_id, tool_name=descriptor.name, arguments=args
            )
        body = _guard(lambda: api.call_tool(
            descriptor.name, args, conversation_id=conversation_id, descriptor=descriptor,
            idempotency_key=key,
        ))
        return body["data"] or {}

    return call


def _description(descriptor: Descriptor) -> str:
    conversation = " Requires conversation_id." if descriptor.needs_conversation else ""
    schema = json.dumps(descriptor.arguments_schema, separators=(",", ":"))
    return (f"{descriptor.description} [{descriptor.level}/{descriptor.effect}]{conversation}"
            f" `arguments` JSON schema: {schema}")


def build_server(api: OdontoflowApi) -> MCPServer:
    try:
        me = api.me()
        if me["principal"]["type"] == "human":
            raise StartupError(
                "HUMAN_TOKEN_REFUSED",
                "odontoflow-mcp runs with an agent credential, never a human one; "
                "use the odontoflow CLI or the UI as a person.",
            )
        descriptors = visible(api.catalog())
    except ApiError as exc:
        raise StartupError(exc.code, exc.message) from None
    except TransportError as exc:
        raise StartupError(exc.code, str(exc)) from None

    server = MCPServer(
        "odontoflow",
        instructions=(
            "OdontoFlow ERP tools for the calling agent. Tools only read or PROPOSE; "
            "a human approves in the inbox. Pass tool arguments inside `arguments`."
        ),
    )
    for descriptor in descriptors:
        server.add_tool(_proxy(api, descriptor), name=descriptor.name,
                        description=_description(descriptor))

    def odontoflow_inbox() -> dict[str, Any]:
        """List the pending approval inbox (proposals waiting for a human)."""
        return _guard(api.inbox)

    def odontoflow_start_cobranza_run() -> dict[str, Any]:
        """Run the collections (cobranza) sweep now; it only proposes reminders."""
        return _guard(lambda: api.start_run("cobranza"))

    def odontoflow_me() -> dict[str, Any]:
        """Who this credential is: principal, organization, roles, permissions."""
        return _guard(api.me)

    for fixed in (odontoflow_inbox, odontoflow_start_cobranza_run, odontoflow_me):
        server.add_tool(fixed, name=fixed.__name__)
    return server
