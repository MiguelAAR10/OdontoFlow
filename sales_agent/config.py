"""Configuration for the optional, API-first Sales Agent process."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.engine import URL, make_url

DEFAULT_AGENT_DATABASE_URL = (
    "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent"
)
CANONICAL_DATABASE_NAMES = frozenset({"odontoflow", "odontoflow_test", "odontoflow_e2e"})
DEFAULT_BACKEND_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL_PROVIDER = "openrouter"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL_PROVIDER = "openai"
SUPPORTED_MODEL_PROVIDERS = frozenset({"openai", "openrouter"})
DEFAULT_RECURSION_LIMIT = 12
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_MODEL_TIMEOUT_SECONDS = 20.0
DEFAULT_TURN_TIMEOUT_SECONDS = 180.0
DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 512
MAX_MODEL_MAX_OUTPUT_TOKENS = 4096


def validate_agent_database_url(raw_url: str) -> URL:
    """Validate that agent memory points to a PostgreSQL database of its own."""
    try:
        url = make_url(raw_url)
    except (TypeError, ValueError) as exc:
        raise ValueError("SALES_AGENT_DATABASE_URL must be a valid PostgreSQL URL.") from exc
    if not url.drivername.startswith("postgresql") or not url.database:
        raise ValueError("SALES_AGENT_DATABASE_URL must name a PostgreSQL database.")
    if url.database in CANONICAL_DATABASE_NAMES:
        raise ValueError(
            "SALES_AGENT_DATABASE_URL must not point at a canonical OdontoFlow database."
        )
    return url


def _positive_int(
    name: str,
    default: int,
    *,
    maximum: int | None = None,
    environ: Mapping[str, str],
) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        bound = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be greater than zero{bound}.")
    return value


def _positive_float(name: str, default: float, *, environ: Mapping[str, str]) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


@dataclass(frozen=True)
class AgentSettings:
    backend_base_url: str
    backend_credential: str | None
    agent_database_url: str
    model: str
    recursion_limit: int
    request_timeout_seconds: float
    model_provider: str = DEFAULT_MODEL_PROVIDER
    model_base_url: str | None = None
    model_api_key: str | None = field(default=None, repr=False)
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS
    model_max_output_tokens: int = DEFAULT_MODEL_MAX_OUTPUT_TOKENS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AgentSettings":
        source = os.environ if environ is None else environ
        database_url = source.get(
            "SALES_AGENT_DATABASE_URL",
            source.get("AGENT_DATABASE_URL", DEFAULT_AGENT_DATABASE_URL),
        )
        validated = validate_agent_database_url(database_url)
        backend_url = source.get(
            "SALES_AGENT_BACKEND_URL",
            source.get("BACKEND_BASE_URL", DEFAULT_BACKEND_BASE_URL),
        ).rstrip("/")

        raw_model = source.get("SALES_AGENT_MODEL", DEFAULT_MODEL).strip()
        configured_provider = source.get("SALES_AGENT_MODEL_PROVIDER")
        configured_provider = configured_provider.strip().lower() if configured_provider else None

        model = raw_model
        prefixed_provider = None
        if ":" in raw_model:
            prefixed_provider, prefixed_model = raw_model.split(":", 1)
            prefixed_provider = prefixed_provider.strip().lower()
            if prefixed_provider in SUPPORTED_MODEL_PROVIDERS:
                model = prefixed_model.strip()
            else:
                prefixed_provider = None
        model_provider = configured_provider or prefixed_provider or DEFAULT_MODEL_PROVIDER
        if model_provider not in SUPPORTED_MODEL_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_MODEL_PROVIDERS))
            raise ValueError(
                f"SALES_AGENT_MODEL_PROVIDER must be one of: {supported}."
            )
        if configured_provider and prefixed_provider and configured_provider != prefixed_provider:
            raise ValueError(
                "SALES_AGENT_MODEL_PROVIDER does not match the provider prefix in "
                "SALES_AGENT_MODEL."
            )
        if not model:
            raise ValueError("SALES_AGENT_MODEL must not be empty.")

        configured_base_url = source.get("SALES_AGENT_MODEL_BASE_URL", "").strip()
        if model_provider == "openrouter":
            model_base_url = (configured_base_url or OPENROUTER_BASE_URL).rstrip("/")
            if model_base_url != OPENROUTER_BASE_URL:
                raise ValueError(
                    "SALES_AGENT_MODEL_BASE_URL must be https://openrouter.ai/api/v1 "
                    "when SALES_AGENT_MODEL_PROVIDER=openrouter."
                )
            model_api_key = source.get("OPENROUTER_API_KEY")
        else:
            model_base_url = configured_base_url.rstrip("/") or None
            model_api_key = source.get("OPENAI_API_KEY")

        return cls(
            backend_base_url=backend_url,
            backend_credential=source.get(
                "SALES_AGENT_V0_CREDENTIAL",
                source.get("SALES_AGENT_CREDENTIAL"),
            ),
            agent_database_url=validated.render_as_string(hide_password=False),
            model=model,
            recursion_limit=_positive_int(
                "SALES_AGENT_RECURSION_LIMIT",
                DEFAULT_RECURSION_LIMIT,
                maximum=100,
                environ=source,
            ),
            request_timeout_seconds=_positive_float(
                "SALES_AGENT_REQUEST_TIMEOUT_SECONDS",
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
                environ=source,
            ),
            model_timeout_seconds=_positive_float(
                "SALES_AGENT_MODEL_TIMEOUT_SECONDS",
                DEFAULT_MODEL_TIMEOUT_SECONDS,
                environ=source,
            ),
            turn_timeout_seconds=_positive_float(
                "SALES_AGENT_TURN_TIMEOUT_SECONDS",
                DEFAULT_TURN_TIMEOUT_SECONDS,
                environ=source,
            ),
            model_max_output_tokens=_positive_int(
                "SALES_AGENT_MODEL_MAX_OUTPUT_TOKENS",
                DEFAULT_MODEL_MAX_OUTPUT_TOKENS,
                maximum=MAX_MODEL_MAX_OUTPUT_TOKENS,
                environ=source,
            ),
            model_provider=model_provider,
            model_base_url=model_base_url,
            model_api_key=model_api_key.strip() if model_api_key else None,
        )


def get_settings() -> AgentSettings:
    return AgentSettings.from_env()
