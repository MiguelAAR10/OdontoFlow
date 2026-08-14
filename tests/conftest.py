import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID, BOOTSTRAP_ORGANIZATION_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

settings = get_settings()
TEST_DATABASE_URL = settings.test_database_url


def _server_url() -> str:
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    url = url.set(database="odontoflow")
    return url.render_as_string(hide_password=False)


def _ensure_test_database() -> None:
    engine = create_engine(_server_url(), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'odontoflow_test'")
        ).scalar()
        if not exists:
            conn.execute(text("CREATE DATABASE odontoflow_test"))
    engine.dispose()


def _reset_schema(url: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _upgrade(url: str) -> None:
    command.upgrade(_alembic_config(url), "head")


@pytest.fixture(scope="session")
def migrated_engine():
    _ensure_test_database()
    _reset_schema(TEST_DATABASE_URL)
    _upgrade(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine):
    yield
    with migrated_engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        tables = (
            "audit_events",
            "appointments",
            "schedule_blocks",
            "availability_rules",
            "practitioner_capabilities",
            "practitioner_memberships",
            "practitioners",
            "locations",
            "services",
            "leads",
            "organizations",
        )
        present = [t for t in tables if t in existing]
        if present:
            conn.execute(
                text("TRUNCATE TABLE " + ", ".join(present) + " RESTART IDENTITY CASCADE")
            )
        if "organizations" in present:
            _seed_bootstrap_organization(conn)


def _seed_bootstrap_organization(conn) -> None:
    """Restore the organization migration ``0002`` seeds, after truncation.

    Every test starts from the same ground the bootstrap migration leaves
    behind: exactly one organization, with the id ``app.tenancy`` resolves to,
    and the identity sequence positioned so a test creating a second tenant gets
    a distinct id.
    """
    conn.execute(
        text("INSERT INTO organizations (id, name) VALUES (:id, :name)"),
        {"id": BOOTSTRAP_ORGANIZATION_ID, "name": BOOTSTRAP_ORGANIZATION_NAME},
    )
    conn.execute(
        text(
            "ALTER TABLE organizations ALTER COLUMN id RESTART WITH "
            f"{BOOTSTRAP_ORGANIZATION_ID + 1}"
        )
    )


@pytest.fixture
def session(migrated_engine):
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    db = maker()
    yield db
    db.close()
