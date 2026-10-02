"""``python -m evals`` — run the Reception scenarios and print ``pass^k``.

    python -m evals                                   # fake model, k=3, exit 0 iff all pass^3
    python -m evals --scenario pedir_humano --k 5
    python -m evals --write-baseline evals/baselines/fake-pass3.json
    python -m evals --mode real --env-file .env.local --write-baseline evals/baselines/real-pass3.json

The fake mode needs PostgreSQL only (``TEST_DATABASE_URL``'s server, 127.0.0.1:5434).
The real mode calls the provider the Sales Agent already uses (OpenRouter by
default); it reads only model-provider variables from the environment or from
``--env-file`` and never prints them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

#: The only variables real mode reads: the model provider, nothing that changes
#: backend behaviour (``DATABASE_URL``, ``APP_ENV``, rate limits…).
MODEL_ENV_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "SALES_AGENT_MODEL",
    "SALES_AGENT_MODEL_PROVIDER",
    "SALES_AGENT_MODEL_BASE_URL",
    "SALES_AGENT_RECURSION_LIMIT",
    "SALES_AGENT_MODEL_TIMEOUT_SECONDS",
    "SALES_AGENT_TURN_TIMEOUT_SECONDS",
    "SALES_AGENT_MODEL_MAX_OUTPUT_TOKENS",
)
#: Placeholder: every trial replaces it with its own throwaway memory database.
_MEMORY_PLACEHOLDER = "postgresql+psycopg://odontoflow@127.0.0.1:5434/odontoflow_eval_memory"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key = key.strip()
        if key in MODEL_ENV_KEYS:
            values[key] = value.strip().strip("'\"")
    return values


def _model_environment(env_file: Path | None) -> dict[str, str]:
    source: Mapping[str, str] = os.environ
    values = {key: source[key] for key in MODEL_ENV_KEYS if source.get(key)}
    if env_file is not None:
        values.update(_read_env_file(env_file))
    values["SALES_AGENT_DATABASE_URL"] = _MEMORY_PLACEHOLDER
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("fake", "real"), default="fake")
    parser.add_argument("--k", type=int, default=3, help="repetitions per scenario (pass^k)")
    parser.add_argument("--scenario", action="append", default=[], help="run only this key")
    parser.add_argument("--anchor-date", type=date.fromisoformat, default=None)
    parser.add_argument("--env-file", type=Path, default=None, help="real mode: model keys only")
    parser.add_argument("--write-baseline", type=Path, default=None)
    parser.add_argument("--server-url", default=None, help="PostgreSQL URL of the eval server")
    return parser


def main(argv: Sequence[str] | None = None, *, scenarios: Sequence | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.k < 1:
        print("--k must be at least 1", file=sys.stderr)
        return 2

    from sales_agent.config import DEFAULT_RECURSION_LIMIT, AgentSettings

    if args.mode == "real":
        try:
            agent_settings = AgentSettings.from_env(_model_environment(args.env_file))
        except (OSError, ValueError) as exc:
            print(f"REAL_MODEL_UNAVAILABLE: {type(exc).__name__}", file=sys.stderr)
            return 2
        if not agent_settings.model_api_key:
            print(
                "REAL_MODEL_UNAVAILABLE: no model provider key in the environment or --env-file.",
                file=sys.stderr,
            )
            return 2
        model_name = f"{agent_settings.model_provider}:{agent_settings.model}"

        def model_factory():
            return None  # the runtime resolves the configured provider

    else:
        from evals.fake_model import FAKE_MODEL_NAME, build_fake_model

        agent_settings = AgentSettings(
            backend_base_url="http://127.0.0.1:8000",
            backend_credential=None,
            agent_database_url=_MEMORY_PLACEHOLDER,
            model=FAKE_MODEL_NAME,
            recursion_limit=DEFAULT_RECURSION_LIMIT,
            request_timeout_seconds=10.0,
        )
        model_name = FAKE_MODEL_NAME

    from evals.runner import run_suite
    from evals.scenarios import SCENARIOS
    from scripts.seed_demo import default_anchor

    selected = list(scenarios if scenarios is not None else SCENARIOS)
    if args.scenario:
        unknown = set(args.scenario) - {scenario.key for scenario in selected}
        if unknown:
            print(f"unknown scenario(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        selected = [scenario for scenario in selected if scenario.key in args.scenario]
    anchor = args.anchor_date or default_anchor()
    if args.mode == "fake":

        def model_factory():
            return build_fake_model(anchor)

    result = run_suite(
        selected,
        k=args.k,
        mode=args.mode,
        model_name=model_name,
        anchor=anchor,
        model_factory=model_factory,
        agent_settings=agent_settings,
        server_url=args.server_url,
        progress=lambda line: print(line, file=sys.stderr, flush=True),
    )
    print(result.report())
    if args.write_baseline is not None:
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(
            json.dumps(result.baseline(), ensure_ascii=False, indent=2) + "\n"
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
