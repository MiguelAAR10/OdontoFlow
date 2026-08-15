import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.iam.models import (
    SYSTEM_PRINCIPAL_DISPLAY_NAME,
    SYSTEM_PRINCIPAL_ID,
    SYSTEM_PRINCIPAL_TYPE,
    SYSTEM_ROLE_CODE,
    SYSTEM_ROLE_NAME,
)
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
            "command_receipts",
            "service_executions",
            "visits",
            "patients",
            "audit_events",
            "appointments",
            "schedule_blocks",
            "availability_rules",
            "practitioner_capabilities",
            "practitioner_memberships",
            "practitioners",
            "role_assignments",
            "role_permissions",
            "roles",
            "memberships",
            "principals",
            "locations",
            "services",
            "leads",
            "organizations",
        )
        # ``permissions`` is deliberately absent: it is the platform catalog,
        # seeded by migration ``0003`` and never runtime data (PF0 §11 M5).
        present = [t for t in tables if t in existing]
        if present:
            conn.execute(
                text("TRUNCATE TABLE " + ", ".join(present) + " RESTART IDENTITY CASCADE")
            )
        if "organizations" in present:
            _seed_bootstrap_organization(conn)
        if "principals" in present:
            _seed_system_principal(conn)


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


def _seed_system_principal(conn) -> None:
    """Restore what migration ``0003`` seeds, after truncation (PF0 PR6/PR7).

    The ``system`` principal plus, in the bootstrap organization, a ``system``
    role holding the whole permission catalog, the membership, and the
    organization-wide role assignment. Platform automation is therefore
    permission-checked on the normal path from the first test statement on, with
    no bypass — the same ground state a freshly migrated database has.
    """
    conn.execute(
        text(
            "INSERT INTO principals (id, type, display_name) VALUES (:id, :type, :name)"
        ),
        {
            "id": SYSTEM_PRINCIPAL_ID,
            "type": SYSTEM_PRINCIPAL_TYPE,
            "name": SYSTEM_PRINCIPAL_DISPLAY_NAME,
        },
    )
    conn.execute(
        text(f"ALTER TABLE principals ALTER COLUMN id RESTART WITH {SYSTEM_PRINCIPAL_ID + 1}")
    )
    conn.execute(
        text("INSERT INTO roles (organization_id, code, name) VALUES (:org, :code, :name)"),
        {
            "org": BOOTSTRAP_ORGANIZATION_ID,
            "code": SYSTEM_ROLE_CODE,
            "name": SYSTEM_ROLE_NAME,
        },
    )
    conn.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.code = :code"
        ),
        {"code": SYSTEM_ROLE_CODE},
    )
    conn.execute(
        text("INSERT INTO memberships (organization_id, principal_id) VALUES (:org, :principal)"),
        {"org": BOOTSTRAP_ORGANIZATION_ID, "principal": SYSTEM_PRINCIPAL_ID},
    )
    conn.execute(
        text(
            "INSERT INTO role_assignments (organization_id, membership_id, role_id) "
            "SELECT m.organization_id, m.id, r.id FROM memberships m "
            "JOIN roles r ON r.organization_id = m.organization_id AND r.code = :code "
            "WHERE m.principal_id = :principal"
        ),
        {"code": SYSTEM_ROLE_CODE, "principal": SYSTEM_PRINCIPAL_ID},
    )


@pytest.fixture
def session(migrated_engine):
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    db = maker()
    yield db
    db.close()
