import uuid
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ALEMBIC_INI, TEST_DATABASE_URL, _alembic_config

EXPECTED_TABLES = {
    "alembic_version",
    "services",
    "locations",
    "practitioners",
    "practitioner_capabilities",
    "leads",
    "availability_rules",
    "schedule_blocks",
    "appointments",
    "audit_events",
}


def _temporary_database_url() -> str:
    name = f"odontoflow_test_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DATABASE_URL).set(database=name)
    return url.render_as_string(hide_password=False)


def test_upgrade_from_empty_database_creates_schema():
    url = _temporary_database_url()
    engine = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {url.rsplit('/', 1)[-1]}"))
    engine.dispose()

    command.upgrade(_alembic_config(url), "head")

    check = create_engine(url)
    with check.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    check.dispose()
    assert tables == EXPECTED_TABLES
    assert version == "0001"


def test_expected_tables_and_constraints_exist(migrated_engine):
    with migrated_engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        constraints = {
            row[0]: row[1]
            for row in conn.execute(
                text(
                    "SELECT conname, contype FROM pg_constraint "
                    "WHERE conrelid = 'appointments'::regclass"
                )
            )
        }
        fk_count = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE contype = 'f' AND conrelid IN "
                "('appointments'::regclass, 'leads'::regclass, "
                "'practitioner_capabilities'::regclass, 'availability_rules'::regclass, "
                "'schedule_blocks'::regclass)"
            )
        ).scalar()
        lead_checks = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'leads'::regclass AND contype = 'c'"
                )
            )
        }

    assert tables == EXPECTED_TABLES
    assert constraints["excl_appointments_confirmed_no_overlap"] == "x"
    assert "ck_appointments_state" in constraints
    assert "ck_appointments_interval" in constraints
    assert fk_count == 12
    assert {"ck_leads_acquisition_source", "ck_leads_at_least_one_contact"} <= lead_checks
    with migrated_engine.connect() as conn:
        ext = conn.execute(text("SELECT extname FROM pg_extension WHERE extname = 'btree_gist'")).scalar()
    assert ext == "btree_gist"


def test_downgrade_returns_to_prior_state(migrated_engine):
    url = TEST_DATABASE_URL
    command.downgrade(_alembic_config(url), "base")
    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).all()
        version_rows = conn.execute(
            text("SELECT count(*) FROM alembic_version")
        ).scalar()
    assert remaining == [("alembic_version",)]
    assert version_rows == 0
    command.upgrade(_alembic_config(url), "head")


def test_reupgrade_after_downgrade_succeeds(migrated_engine, clean_tables):
    url = TEST_DATABASE_URL
    command.upgrade(_alembic_config(url), "head")
    with migrated_engine.connect() as conn:
        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
    assert version == "0001"
    assert tables == EXPECTED_TABLES
