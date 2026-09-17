#!/usr/bin/env python3
"""Report the credential-free local Sales Agent readiness boundary."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sales_agent.config import OPENROUTER_BASE_URL, AgentSettings  # noqa: E402

DEFAULT_ENV_FILE = REPO_ROOT / ".env.local"
EXPECTED_PROVIDER = "openrouter"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse the simple shell assignments used by the repository env files."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator or not key.isidentifier():
            continue
        try:
            parsed = shlex.split(raw_value, comments=False)
        except ValueError:
            continue
        values[key] = parsed[0] if parsed else ""
    return values


def load_local_env(path: Path = DEFAULT_ENV_FILE) -> None:
    """Load current local-file values without printing or clobbering shell secrets."""
    for key, value in _parse_env_file(path).items():
        if value or not os.environ.get(key, "").strip():
            os.environ[key] = value


def _configured(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sandbox_configured(environ: Mapping[str, str]) -> bool:
    required = (
        "SANDBOX_BACKEND_URL",
        "SANDBOX_RECEIVER_URL",
        "SANDBOX_DISPATCHER_TOKEN",
        "SANDBOX_INBOUND_TOKEN",
    )
    return all(_configured(environ.get(name)) for name in required)


def collect_preflight(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return sanitized readiness values without opening a network connection."""
    source = dict(os.environ if environ is None else environ)
    try:
        settings = AgentSettings.from_env(source)
        provider = settings.model_provider
        model = settings.model
        base_url_configured = settings.model_base_url == OPENROUTER_BASE_URL
    except ValueError:
        provider = source.get("SALES_AGENT_MODEL_PROVIDER", "").strip().lower() or "invalid"
        model = source.get("SALES_AGENT_MODEL", "").strip() or "invalid"
        base_url_configured = False

    provider_configured = provider == EXPECTED_PROVIDER
    api_key_configured = _configured(source.get("OPENROUTER_API_KEY"))
    sales_agent_credential_configured = _configured(
        source.get("SALES_AGENT_V0_CREDENTIAL")
    )
    postgres_configured = all(
        _configured(source.get(name))
        for name in ("DATABASE_URL", "SALES_AGENT_DATABASE_URL")
    )
    sandbox_configured = _sandbox_configured(source)
    ready = all(
        (
            provider_configured,
            _configured(model),
            base_url_configured,
            api_key_configured,
            sales_agent_credential_configured,
            postgres_configured,
            sandbox_configured,
        )
    )
    return {
        "provider": provider.replace("\n", " "),
        "model": model.replace("\n", " "),
        "base_url_configured": str(base_url_configured).lower(),
        "openrouter_api_key_configured": str(api_key_configured).lower(),
        "sales_agent_credential_configured": str(
            sales_agent_credential_configured
        ).lower(),
        "postgres_configured": str(postgres_configured).lower(),
        "sandbox_configured": str(sandbox_configured).lower(),
        "ready_for_real_model_smoke": str(ready).lower(),
    }


def render_preflight(values: Mapping[str, str]) -> str:
    order = (
        "provider",
        "model",
        "base_url_configured",
        "openrouter_api_key_configured",
        "sales_agent_credential_configured",
        "postgres_configured",
        "sandbox_configured",
        "ready_for_real_model_smoke",
    )
    return "\n".join(f"{key}={values[key]}" for key in order)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args()
    load_local_env(args.env_file)
    print(render_preflight(collect_preflight()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
