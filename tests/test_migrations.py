import uuid
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ALEMBIC_INI, TEST_DATABASE_URL, _alembic_config

EXPECTED_TABLES = {
    "alembic_version",
    "organizations",
    "practitioner_memberships",
    # PF2 — principal identity and authorization (migration 0003).
    "principals",
    "memberships",
    "permissions",
    "roles",
    "role_permissions",
    "role_assignments",
    # PF4 — durable command idempotency (migration 0004).
    "command_receipts",
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

HEAD_REVISION = "0004"

# The eight tables that gained direct tenant ownership in PF1 (PF0 T1).
TENANT_OWNED_TABLES = (
    "services",
    "locations",
    "leads",
    "practitioner_capabilities",
    "availability_rules",
    "schedule_blocks",
    "appointments",
    "audit_events",
)


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
    assert version == HEAD_REVISION


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
    # 12 Vertical 1 keys + 5 plain organization keys + 12 composite tenant keys.
    assert fk_count == 29
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
    assert version == HEAD_REVISION
    assert tables == EXPECTED_TABLES


# --- PF1: migration 0002 over a database holding Vertical 1 rows ------------


LEGACY_ROWS = (
    "INSERT INTO services (name, duration_minutes) VALUES ('Limpieza', 30)",
    "INSERT INTO locations (name, timezone) VALUES ('Sede Centro', 'America/Lima')",
    "INSERT INTO practitioners (display_name) VALUES ('Dra. Ana')",
    "INSERT INTO leads (full_name, contact_phone, acquisition_source, service_need_id)"
    " VALUES ('Juan', '+51999000111', 'direct', 1)",
    "INSERT INTO practitioner_capabilities (practitioner_id, service_id, location_id)"
    " VALUES (1, 1, 1)",
    "INSERT INTO availability_rules (practitioner_id, location_id, day_of_week,"
    " start_local, end_local) VALUES (1, 1, 0, '09:00', '13:00')",
    "INSERT INTO schedule_blocks (practitioner_id, location_id, start_utc, end_utc)"
    " VALUES (1, 1, '2026-08-10T14:00:00+00', '2026-08-10T15:00:00+00')",
    "INSERT INTO appointments (lead_id, service_id, practitioner_id, location_id,"
    " start_utc, end_utc, state)"
    " VALUES (1, 1, 1, 1, '2026-08-10T16:00:00+00', '2026-08-10T16:30:00+00', 'confirmed')",
    "INSERT INTO audit_events (actor_id, actor_type, action, entity_id, entity_type)"
    " VALUES ('system', 'system', 'appointment.created', '1', 'appointment')",
)


@pytest.fixture
def legacy_database():
    """A throwaway database at revision ``0001`` holding one row per table."""
    url = _temporary_database_url()
    name = url.rsplit("/", 1)[-1]
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with server.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))

    command.upgrade(_alembic_config(url), "0001")
    engine = create_engine(url)
    with engine.begin() as conn:
        for statement in LEGACY_ROWS:
            conn.execute(text(statement))
    try:
        yield url, engine
    finally:
        engine.dispose()
        with server.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        server.dispose()


def test_upgrade_backfills_existing_rows_into_the_bootstrap_organization(
    legacy_database,
):
    url, engine = legacy_database

    command.upgrade(_alembic_config(url), HEAD_REVISION)

    with engine.connect() as conn:
        organizations = conn.execute(
            text("SELECT id, name FROM organizations ORDER BY id")
        ).all()
        assert organizations == [(1, "Bootstrap Clinic")]

        for table in TENANT_OWNED_TABLES:
            rows, owned, tenants = conn.execute(
                text(
                    f"SELECT count(*), count(organization_id),"
                    f" count(DISTINCT organization_id) FROM {table}"
                )
            ).one()
            assert rows == 1, table
            assert owned == 1, table  # nothing left NULL
            assert tenants == 1, table

        assert conn.execute(
            text("SELECT count(*) FROM appointments WHERE organization_id = 1")
        ).scalar() == 1
        # One membership per pre-existing practitioner, active.
        assert conn.execute(
            text(
                "SELECT organization_id, practitioner_id, is_active"
                " FROM practitioner_memberships"
            )
        ).all() == [(1, 1, True)]
        # Every tenant column is NOT NULL after the backfill.
        nullable = conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name = 'organization_id'"
                " AND is_nullable = 'YES'"
            )
        ).all()
        assert nullable == []
        # The tenant constraints of PF0 §7.2 exist, the global name UNIQUE is gone,
        # and the practitioner-global GiST is byte-for-byte unchanged.
        constraints = {
            row[0]
            for row in conn.execute(
                text("SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace")
            )
        }
        assert {
            "uq_services_organization_name",
            "uq_services_organization_id",
            "uq_locations_organization_id",
            "uq_leads_organization_id",
            "uq_appointments_organization_id",
            "uq_practitioner_memberships_org_practitioner",
            "uq_practitioner_memberships_org_id",
            "fk_appointments_organization_lead",
            "fk_appointments_organization_service",
            "fk_appointments_organization_membership",
            "fk_appointments_organization_location",
            "fk_capabilities_organization_membership",
            "fk_capabilities_organization_service",
            "fk_capabilities_organization_location",
            "fk_availability_rules_organization_membership",
            "fk_availability_rules_organization_location",
            "fk_schedule_blocks_organization_membership",
            "fk_schedule_blocks_organization_location",
            "fk_leads_organization_service_need",
            "fk_audit_events_organization",
        } <= constraints
        assert "services_name_key" not in constraints
        assert "uq_capabilities_practitioner_service_location" in constraints
        exclusion = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'excl_appointments_confirmed_no_overlap'"
            )
        ).scalar()
    assert exclusion == (
        "EXCLUDE USING gist (practitioner_id WITH =,"
        " tstzrange(start_utc, end_utc, '[)'::text) WITH &&)"
        " WHERE (((state)::text = 'confirmed'::text))"
    )
    assert "organization_id" not in exclusion


def test_downgrade_and_reupgrade_preserve_existing_rows(legacy_database):
    url, engine = legacy_database
    config = _alembic_config(url)

    command.upgrade(config, HEAD_REVISION)
    command.downgrade(config, "0001")

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        assert "organizations" not in tables
        assert "practitioner_memberships" not in tables
        tenant_columns = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name = 'organization_id'"
            )
        ).scalar()
        assert tenant_columns == 0
        service_uniques = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = 'services'::regclass AND contype = 'u'"
                )
            )
        }
        assert service_uniques == {"services_name_key"}
        # Not a single Vertical 1 row was discarded on the way down.
        assert conn.execute(text("SELECT count(*) FROM appointments")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar() == 1

    command.upgrade(config, HEAD_REVISION)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == HEAD_REVISION
        assert conn.execute(
            text("SELECT organization_id FROM appointments")
        ).scalar() == 1
        assert conn.execute(
            text("SELECT count(*) FROM practitioner_memberships")
        ).scalar() == 1
