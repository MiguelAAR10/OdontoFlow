import os
from dataclasses import dataclass

DEFAULT_APP_ENV = "development"
DEFAULT_DATABASE_URL = "postgresql+psycopg://odontoflow:odontoflow@localhost:5434/odontoflow"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://odontoflow:odontoflow@localhost:5434/odontoflow_test"


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    test_database_url: str


def get_settings() -> Settings:
    return Settings(
        app_env=os.environ.get("APP_ENV", DEFAULT_APP_ENV),
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        test_database_url=os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
    )
