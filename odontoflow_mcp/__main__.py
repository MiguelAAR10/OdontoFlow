"""`odontoflow-mcp` entry point. Errors go to stderr only; stdout is JSON-RPC."""

from __future__ import annotations

import argparse
import os
import sys

from odontoflow_cli.api import DEFAULT_URL, OdontoflowApi
from odontoflow_mcp.server import StartupError, build_server


def _fail(code: str, message: str) -> int:
    sys.stderr.write(f"{code}: {message}\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="odontoflow-mcp")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=3001,
                        help="streamable-http only; always bound to 127.0.0.1")
    args = parser.parse_args(argv)

    token = os.environ.get("ODONTOFLOW_TOKEN", "").strip()
    if not token:
        return _fail("CONFIG_MISSING", "set ODONTOFLOW_TOKEN to an agent credential.")
    api = OdontoflowApi(os.environ.get("ODONTOFLOW_URL") or DEFAULT_URL, token)
    try:
        server = build_server(api)
    except StartupError as exc:
        return _fail(exc.code, exc.message)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        # Opt-in, untested. Fixed loopback host keeps the SDK's default
        # DNS-rebinding protection; there is no caller authentication.
        server.run("streamable-http", host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
