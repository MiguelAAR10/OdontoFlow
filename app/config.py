import os
from dataclasses import dataclass

DEFAULT_APP_ENV = "development"
DEFAULT_DATABASE_URL = "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_test"
DEFAULT_MAX_JSON_BODY_BYTES = 256 * 1024
DEFAULT_RATE_LIMIT_READS_PER_MINUTE = 600
DEFAULT_RATE_LIMIT_MUTATIONS_PER_MINUTE = 120
DEFAULT_MESSAGE_CONTENT_RETENTION_DAYS = 90


def _boolean_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _origins_env() -> tuple[str, ...]:
    origins = tuple(
        item.strip()
        for item in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )
    if "*" in origins:
        raise ValueError("CORS_ALLOWED_ORIGINS cannot contain a wildcard.")
    return origins


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    test_database_url: str
    integration_api_enabled: bool
    max_json_body_bytes: int
    rate_limit_reads_per_minute: int
    rate_limit_mutations_per_minute: int
    cors_allowed_origins: tuple[str, ...]
    require_https: bool
    message_content_retention_days: int


def get_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", DEFAULT_APP_ENV).strip().lower()
    return Settings(
        app_env=app_env,
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        test_database_url=os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
        integration_api_enabled=_boolean_env("INTEGRATION_API_ENABLED", True),
        max_json_body_bytes=_positive_int_env(
            "MAX_JSON_BODY_BYTES", DEFAULT_MAX_JSON_BODY_BYTES
        ),
        rate_limit_reads_per_minute=_positive_int_env(
            "RATE_LIMIT_READS_PER_MINUTE", DEFAULT_RATE_LIMIT_READS_PER_MINUTE
        ),
        rate_limit_mutations_per_minute=_positive_int_env(
            "RATE_LIMIT_MUTATIONS_PER_MINUTE",
            DEFAULT_RATE_LIMIT_MUTATIONS_PER_MINUTE,
        ),
        cors_allowed_origins=_origins_env(),
        require_https=_boolean_env("REQUIRE_HTTPS", app_env == "production"),
        message_content_retention_days=_positive_int_env(
            "MESSAGE_CONTENT_RETENTION_DAYS",
            DEFAULT_MESSAGE_CONTENT_RETENTION_DAYS,
        ),
    )
