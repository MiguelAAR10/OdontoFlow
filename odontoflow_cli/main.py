"""`odontoflow` — the team's terminal door to the same HTTP API the UI uses.

Config: ``ODONTOFLOW_TOKEN`` (required, never printed) and ``ODONTOFLOW_URL``
(default ``http://127.0.0.1:8000``). Exit codes: 0 ok, 1 API error, 2 usage or
config, 3 transport. ``--json`` prints the exact server body.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

from odontoflow_cli.api import DEFAULT_URL, ApiError, OdontoflowApi, TransportError, visible

EXIT_OK, EXIT_API, EXIT_USAGE, EXIT_TRANSPORT = 0, 1, 2, 3


class UsageError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the exact server JSON body")
    parser = argparse.ArgumentParser(prog="odontoflow", parents=[common],
                                     description="OdontoFlow ERP over its HTTP API.")
    sub = parser.add_subparsers(dest="command", required=True)

    tools = sub.add_parser("tools", help="agent tool catalog").add_subparsers(
        dest="tools_command", required=True)
    tools.add_parser("list", parents=[common], help="tools this token may call (never L4)")

    call = sub.add_parser("call", parents=[common], help="call one catalog tool")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="tool arguments as a JSON object")
    call.add_argument("--conversation", type=int, default=None,
                      help="conversation id (required by tools with needs_conversation)")

    sub.add_parser("inbox", parents=[common], help="pending approval inbox")

    approve = sub.add_parser("approve", parents=[common], help="approve a proposal (humans)")
    approve.add_argument("proposal_id", type=int)
    approve.add_argument("--hash", required=True, dest="payload_hash")
    approve.add_argument("--note", default=None)
    approve.add_argument("--idempotency-key", default=None,
                         help="reuse a key to replay the same approval")

    decline = sub.add_parser("decline", parents=[common], help="decline a proposal (humans)")
    decline.add_argument("proposal_id", type=int)
    decline.add_argument("--note", default=None)

    runs = sub.add_parser("runs", help="agent runs").add_subparsers(
        dest="runs_command", required=True)
    start = runs.add_parser("start", parents=[common], help="run an agent now")
    start.add_argument("agent_key", choices=["cobranza"])

    jobs = sub.add_parser("jobs", help="agent jobs").add_subparsers(
        dest="jobs_command", required=True)
    run_due = jobs.add_parser("run-due", parents=[common],
                              help="enqueue, claim and handle due agent jobs once")
    run_due.add_argument("--limit", type=int, default=None, help="max jobs to claim (1-20)")

    sub.add_parser("me", parents=[common], help="who this token is")
    return parser


# --- human output --------------------------------------------------------------


def _text(command: str, body: Any) -> str:
    if command == "tools":
        rows = [f"{'name':<28} {'effect':<8} {'level':<5} needs_conversation"]
        rows += [f"{t['name']:<28} {t['effect']:<8} {t['level']:<5} {t['needs_conversation']}"
                 for t in body["tools"]]
        return "\n".join(rows)
    if command == "call":
        return json.dumps(body["data"], indent=2, ensure_ascii=False, default=str)
    if command == "inbox":
        rows = [f"{i['id']}\t{i['kind']}\t{i['status']}\t{i.get('payload_hash') or '-'}\t"
                f"{i.get('summary') or ''}" for i in body["items"]]
        return "\n".join(rows) or "(inbox empty)"
    if command in ("approve", "decline"):
        return f"proposal {body['id']}: {body['status']}"
    if command == "runs":
        counts = " ".join(f"{k}={v}" for k, v in body["counts"].items())
        return f"run {body['id']} {body['agent_key']}: {body['status']} {counts}"
    if command == "jobs":
        counts = " ".join(f"{k}={body[k]}" for k in
                          ("enqueued", "claimed", "done", "failed", "dead", "lost"))
        disabled = ",".join(body["disabled_agents"]) or "-"
        return f"jobs: {counts} disabled={disabled}"
    if command == "me":
        p, org = body["principal"], body["organization"]
        return (f"{p['display_name']} ({p['type']} #{p['id']}) @ {org['name']}\n"
                f"permissions: {', '.join(body['permissions'])}")
    return json.dumps(body, ensure_ascii=False)


def _dispatch(api: OdontoflowApi, args: argparse.Namespace) -> Any:
    command = args.command
    if command == "tools":
        return {"tools": [vars(d) for d in visible(api.catalog())]}
    if command == "call":
        try:
            arguments = json.loads(args.args)
        except json.JSONDecodeError as exc:
            raise UsageError(f"--args is not valid JSON: {exc.msg}") from None
        if not isinstance(arguments, dict):
            raise UsageError("--args must be a JSON object")
        descriptor = next((d for d in api.catalog() if d.name == args.tool), None)
        return api.call_tool(args.tool, arguments, conversation_id=args.conversation,
                             descriptor=descriptor)
    if command == "inbox":
        return api.inbox()
    if command == "approve":
        return api.approve(args.proposal_id, payload_hash=args.payload_hash, note=args.note,
                           idempotency_key=args.idempotency_key)
    if command == "decline":
        return api.decline(args.proposal_id, note=args.note)
    if command == "runs":
        return api.start_run(args.agent_key)
    if command == "jobs":
        return api.run_due_jobs(limit=args.limit)
    return api.me()


def main(argv: list[str] | None = None, *, http_client: httpx.Client | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already wrote the usage to stderr
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    as_json = getattr(args, "json", False)

    token = os.environ.get("ODONTOFLOW_TOKEN", "").strip()
    if not token:
        print("CONFIG_MISSING: set ODONTOFLOW_TOKEN to an OdontoFlow credential.",
              file=sys.stderr)
        return EXIT_USAGE
    api = OdontoflowApi(os.environ.get("ODONTOFLOW_URL") or DEFAULT_URL, token,
                        http_client=http_client)
    try:
        body = _dispatch(api, args)
    except UsageError as exc:
        print(f"USAGE_ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ApiError as exc:
        if as_json:
            print(json.dumps(exc.body, ensure_ascii=False))
        else:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_API
    except TransportError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT
    print(json.dumps(body, ensure_ascii=False) if as_json else _text(args.command, body))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
