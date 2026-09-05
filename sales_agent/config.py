"""Configuration for the optional, API-first Sales Agent process."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url

DEFAULT_AGENT_DATABASE_URL = (
    "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent"
)
CANONICAL_DATABASE_NAMES = frozenset({"odontoflow", "odontoflow_test", "odontoflow_e2e"})
DEFAULT_BACKEND_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "openai:gpt-5.4-mini"
DEFAULT_RECURSION_LIMIT = 12
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0


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


def _positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        bound = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be greater than zero{bound}.")
    return value


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if value <= 0:
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

    @classmethod
    def from_env(cls) -> "AgentSettings":
        database_url = os.environ.get(
            "SALES_AGENT_DATABASE_URL",
            os.environ.get("AGENT_DATABASE_URL", DEFAULT_AGENT_DATABASE_URL),
        )
        validated = validate_agent_database_url(database_url)
        backend_url = os.environ.get(
            "SALES_AGENT_BACKEND_URL",
            os.environ.get("BACKEND_BASE_URL", DEFAULT_BACKEND_BASE_URL),
        ).rstrip("/")
        return cls(
            backend_base_url=backend_url,
            backend_credential=os.environ.get(
                "SALES_AGENT_V0_CREDENTIAL",
                os.environ.get("SALES_AGENT_CREDENTIAL"),
            ),
            agent_database_url=validated.render_as_string(hide_password=False),
            model=os.environ.get("SALES_AGENT_MODEL", DEFAULT_MODEL),
            recursion_limit=_positive_int(
                "SALES_AGENT_RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT, maximum=100
            ),
            request_timeout_seconds=_positive_float(
                "SALES_AGENT_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
        )


def get_settings() -> AgentSettings:
    return AgentSettings.from_env()
