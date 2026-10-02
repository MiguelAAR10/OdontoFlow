import uuid
from decimal import Decimal
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
    # PF5 — clinical core (migration 0005).
    "patients",
    "visits",
    "service_executions",
    # PF6 — economic & operations core (migration 0006).
    "products",
    "service_consumptions",
    "charges",
    "payments",
    # FE3A — deterministic collection follow-ups (migration 0018).
    "charge_follow_ups",
    # PF7 — inventory ledger (migration 0007).
    "integration_credentials",
    "integration_rate_limits",
    "security_events",
    "channel_accounts",
    "contact_identities",
    "conversations",
    "messages",
    "outbound_messages",
    "sandbox_delivery_receipts",
    "appointment_cancellation_proposals",
    "inventory_movements",
    "services",
    "locations",
    "practitioners",
    "practitioner_capabilities",
    "leads",
    "availability_rules",
    "schedule_blocks",
    "appointments",
    "appointment_proposals",
    "appointment_reschedule_proposals",
    "promotions",
    "reception_handoffs",
    "audit_events",
    # B0.5 — domain gaps (migration 0020).
    "domain_events",
    "payment_reversals",
    "reorder_points",
    "waitlist_entries",
    # B2 — generic agent proposals (migration 0021).
    "agent_proposals",
    # COB — agent runs (migration 0022).
    "agent_runs",
    # BACKFILL — leased agent jobs (migration 0026).
    "agent_jobs",
}

HEAD_REVISION = "0026"

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


def test_in_process_migration_command_leaves_existing_loggers_enabled():
    """env.py's ``fileConfig`` must not disable loggers created before it runs."""
    import logging

    existing = logging.getLogger(f"odontoflow.test.preexisting.{uuid.uuid4().hex[:8]}")
    assert existing.disabled is False

    command.current(_alembic_config(TEST_DATABASE_URL))

    assert existing.disabled is False


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
    # Phase 5 adds the contact-bound patient link on appointments.
    assert fk_count == 30
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
        # Every tenant-owned column is NOT NULL after the backfill.
        # ``security_events.organization_id`` is intentionally nullable:
        # rejected credentials have no trusted tenant to record.
        nullable = conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name = 'organization_id'"
                " AND is_nullable = 'YES'"
            )
        ).all()
        assert nullable == [("security_events",)]
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


# --- M4.2: migration 0008 (location-aware inventory) -------------------------


INVENTORY_LEDGER_ROWS = (
    "INSERT INTO locations (organization_id, name, timezone)"
    " VALUES (1, 'Sede Uno', 'America/Lima')",
    "INSERT INTO locations (organization_id, name, timezone)"
    " VALUES (1, 'Sede Dos', 'America/Lima')",
    "INSERT INTO services (organization_id, name, duration_minutes)"
    " VALUES (1, 'Limpieza', 30)",
    "INSERT INTO practitioners (display_name) VALUES ('Dra. Ana')",
    "INSERT INTO practitioner_memberships (organization_id, practitioner_id, is_active)"
    " VALUES (1, 1, true)",
    "INSERT INTO patients (organization_id, full_name, dni, sexo)"
    " VALUES (1, 'Paciente Uno', '12345678', 'M')",
    "INSERT INTO visits (organization_id, patient_id, practitioner_id, location_id)"
    " VALUES (1, 1, 1, 1)",
    "INSERT INTO service_executions (organization_id, visit_id, service_id, executed_price)"
    " VALUES (1, 1, 1, 150.00)",
    "INSERT INTO products (organization_id, name, unit, kind)"
    " VALUES (1, 'Anestesia', 'ampolla', 'consumible')",
    "INSERT INTO service_consumptions"
    " (organization_id, service_execution_id, product_id, quantity, unit_price)"
    " VALUES (1, 1, 1, 2.00, 25.00)",
    # The SALIDA is causally linked to the consumption (1:1, 0007 contract).
    "INSERT INTO inventory_movements (organization_id, product_id, type, quantity, id_consumo_origen)"
    " VALUES (1, 1, 'SALIDA', 2.00, 1)",
)


@pytest.fixture
def inventory_ledger_database(extra_movements=()):
    """A throwaway database at revision ``0007`` holding a consumption chain.

    The SALIDA row is the only pre-existing movement by default; ``extra_movements``
    adds org-level rows with no consumption origin (the rows a location backfill
    must never fabricate a location for).
    """
    url = _temporary_database_url()
    name = url.rsplit("/", 1)[-1]
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with server.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))

    command.upgrade(_alembic_config(url), "0007")
    engine = create_engine(url)
    with engine.begin() as conn:
        for statement in INVENTORY_LEDGER_ROWS + tuple(extra_movements):
            conn.execute(text(statement))
    try:
        yield url, engine
    finally:
        engine.dispose()
        with server.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        server.dispose()


def test_upgrade_0008_backfills_consumption_linked_rows_into_their_visit_location(
    inventory_ledger_database,
):
    url, engine = inventory_ledger_database

    command.upgrade(_alembic_config(url), HEAD_REVISION)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == HEAD_REVISION
        # The SALIDA's location is derived truthfully from its consumption's
        # visit — never guessed.
        rows = conn.execute(
            text(
                "SELECT location_id, type, id_consumo_origen"
                " FROM inventory_movements ORDER BY id"
            )
        ).all()
        assert rows == [(1, "SALIDA", 1)]
        nullable = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'inventory_movements'"
                " AND column_name = 'location_id' AND is_nullable = 'YES'"
            )
        ).scalar()
        assert nullable == 0
        # The composite FK into locations(organization_id, id) exists.
        fk = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'fk_inventory_movements_organization_location'"
            )
        ).scalar()
        assert fk == (
            "FOREIGN KEY (organization_id, location_id)"
            " REFERENCES locations(organization_id, id) ON DELETE RESTRICT"
        )


def test_upgrade_0008_refuses_to_fabricate_org_level_locations(
    inventory_ledger_database,
):
    url, engine = inventory_ledger_database
    config = _alembic_config(url)
    # An org-level ENTRADA has no consumption origin: no truthful location.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inventory_movements (organization_id, product_id, type, quantity)"
                " VALUES (1, 1, 'ENTRADA', 10.00)"
            )
        )

    with pytest.raises(Exception) as exc:
        command.upgrade(config, HEAD_REVISION)
    assert "refuses to fabricate" in str(exc.value).lower()

    # The failed upgrade is atomic: still at 0007, nothing altered.
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == "0007"
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'inventory_movements'"
                )
            )
        }
        assert "location_id" not in columns  # the failed migration left no trace
        assert conn.execute(
            text("SELECT count(*) FROM inventory_movements")
        ).scalar() == 2

    # The operator resolves the org-level row explicitly (it cannot be
    # fabricated: it is removed), then the upgrade runs and backfills only
    # what is determinable.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM inventory_movements WHERE id_consumo_origen IS NULL")
        )
    command.upgrade(config, HEAD_REVISION)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT location_id, type FROM inventory_movements ORDER BY id"
            )
        ).all()
        # The consumption-linked row is derived from its visit — the only
        # truthful location. Nothing was ever guessed.
        assert rows == [(1, "SALIDA")]


def test_downgrade_0008_restores_0007_and_reupgrade_rederives_locations(
    inventory_ledger_database,
):
    url, engine = inventory_ledger_database
    config = _alembic_config(url)

    command.upgrade(config, HEAD_REVISION)
    # A valid transfer pair at 0008: Sede Uno → Sede Dos.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (1, 1, 1, 'TRANSFER_OUT', 1.00, 't' || repeat('0', 35))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO inventory_movements"
                " (organization_id, product_id, location_id, type, quantity, transfer_id)"
                " VALUES (1, 1, 2, 'TRANSFER_IN', 1.00, 't' || repeat('0', 35))"
            )
        )

    command.downgrade(config, "0007")

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == "0007"
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = 'inventory_movements'"
                )
            )
        }
        assert "location_id" not in columns
        assert "transfer_id" not in columns
        # Transfer rows cannot exist at 0007 (the old type CHECK forbids them);
        # the SALIDA survives with its causal link intact.
        rows = conn.execute(
            text(
                "SELECT type, quantity, id_consumo_origen"
                " FROM inventory_movements ORDER BY id"
            )
        ).all()
        assert rows == [("SALIDA", Decimal("2.00"), 1)]

    command.upgrade(config, HEAD_REVISION)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == HEAD_REVISION
        # The surviving SALIDA re-derives its location from the consumption
        # chain — the strategy is truthful across cycles.
        rows = conn.execute(
            text("SELECT location_id, type FROM inventory_movements ORDER BY id")
        ).all()
        assert rows == [(1, "SALIDA")]


# --- B2: migration 0021 (agent_proposals) on a disposable database ----------

B2_CODES = ("proposals.read", "proposals.create", "proposals.decide")


def _b2_disposable_url() -> str:
    url = _temporary_database_url()
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with server.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {url.rsplit('/', 1)[-1]}"))
    server.dispose()
    return url


def _b2_drop(url: str) -> None:
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with server.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {url.rsplit('/', 1)[-1]} WITH (FORCE)"))
    server.dispose()


def _b2_state(engine):
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        codes = set(
            conn.execute(
                text("SELECT code FROM permissions WHERE code = ANY(:codes)"),
                {"codes": list(B2_CODES)},
            ).scalars()
        )
    return tables, codes


def _b2_insert(conn, *, status="pending", kind="collection_reminder"):
    conn.execute(
        text(
            "INSERT INTO agent_proposals (organization_id, agent_key, kind, status, payload, "
            "payload_hash, subject_type, subject_id, subject_version, reason, dedupe_key, "
            "execution_key, proposed_by_principal_id, expires_at) VALUES (1, 'reception', "
            ":kind, :status, '{}'::jsonb, repeat('a', 64), 'charge', '1', '0', 'motivo', "
            ":dedupe, gen_random_uuid(), 1, now() + interval '1 day')"
        ),
        {"kind": kind, "status": status, "dedupe": f"{kind}:charge:{uuid.uuid4()}"},
    )


def test_migration_0021_round_trip_and_checks():
    from sqlalchemy.exc import IntegrityError

    url = _b2_disposable_url()
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, "0020")
        tables, codes = _b2_state(engine)
        assert "agent_proposals" not in tables and codes == set()

        command.upgrade(config, "0021")
        tables, codes = _b2_state(engine)
        assert "agent_proposals" in tables and codes == set(B2_CODES)
        with engine.begin() as conn:
            _b2_insert(conn)  # the system principal (id 1) is a member of org 1
        for bad in ({"status": "running"}, {"kind": "inventory_transfer"}):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    _b2_insert(conn, **bad)
        with pytest.raises(IntegrityError):  # approved needs a decider
            with engine.begin() as conn:
                _b2_insert(conn, status="approved")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_proposals"))

        command.downgrade(config, "0020")
        tables, codes = _b2_state(engine)
        assert "agent_proposals" not in tables and codes == set()
        command.upgrade(config, "0021")
        assert "agent_proposals" in _b2_state(engine)[0]
    finally:
        engine.dispose()
        _b2_drop(url)


# --- COB: migration 0022 (agent_runs) on a disposable database ---------------


def _cob_insert(conn, **overrides):
    row = {
        "agent_key": "cobranza",
        "trigger": "manual",
        "status": "running",
        "candidates": 0,
        "proposed": 0,
        "deduped": 0,
        "skipped": 0,
        "error": None,
        "finished": None,
    }
    row.update(overrides)
    conn.execute(
        text(
            "INSERT INTO agent_runs (organization_id, agent_key, trigger, status, "
            "triggered_by_principal_id, candidates_count, proposed_count, deduped_count, "
            "skipped_count, error_category, finished_at) VALUES (1, :agent_key, :trigger, "
            ":status, 1, :candidates, :proposed, :deduped, :skipped, :error, "
            "CASE WHEN :finished THEN now() ELSE NULL END)"
        ),
        {**row, "finished": bool(row["finished"])},
    )


def test_migration_0022_round_trip_and_checks():
    from sqlalchemy.exc import IntegrityError

    url = _b2_disposable_url()
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, "0021")
        assert "agent_runs" not in _b2_state(engine)[0]

        command.upgrade(config, "0022")
        assert "agent_runs" in _b2_state(engine)[0]
        with engine.begin() as conn:
            _cob_insert(conn)  # the system principal (id 1) is a member of org 1
            _cob_insert(
                conn, status="completed", finished=True, candidates=3, proposed=1,
                deduped=1, skipped=1,
            )
            _cob_insert(conn, status="failed", finished=True, error="unexpected")
        bad_rows = (
            {"status": "paused"},
            {"trigger": "cron"},
            {"agent_key": "inventario"},
            {"status": "completed", "finished": True, "candidates": 2, "proposed": 1},
            {"status": "completed"},  # finished_at is required once not running
            {"status": "running", "finished": True},
            {"status": "failed", "finished": True},  # failed needs an error_category
            {"status": "completed", "finished": True, "error": "unexpected"},
            {"skipped": -1},
        )
        for bad in bad_rows:
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    _cob_insert(conn, **bad)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs"))

        command.downgrade(config, "0021")
        assert "agent_runs" not in _b2_state(engine)[0]
        command.upgrade(config, "0022")
        assert "agent_runs" in _b2_state(engine)[0]
    finally:
        engine.dispose()
        _b2_drop(url)


# --- B3: migration 0023 (reception agent runs) on a disposable database -------


def _b3_seed_message(conn) -> tuple[int, int, int]:
    """One channel/contact with two conversations; returns (conv_a, msg_a, msg_b)."""
    channel = conn.execute(
        text(
            "INSERT INTO channel_accounts (organization_id, provider, external_account_id, "
            "display_name, is_active) VALUES (1, 'whatsapp', 'wa-0023', 'WA', true) RETURNING id"
        )
    ).scalar_one()
    ids = []
    for n in range(2):
        contact = conn.execute(
            text(
                "INSERT INTO contact_identities (organization_id, channel_account_id, "
                "external_contact_id, normalized_phone_e164, consent_status) VALUES "
                "(1, :ch, :ext, :phone, 'opted_in') RETURNING id"
            ),
            {"ch": channel, "ext": f"c-0023-{n}", "phone": f"+5198000000{n}"},
        ).scalar_one()
        conv = conn.execute(
            text(
                "INSERT INTO conversations (organization_id, channel_account_id, "
                "contact_identity_id, status, last_message_at) VALUES (1, :ch, :ct, 'open', "
                "now()) RETURNING id"
            ),
            {"ch": channel, "ct": contact},
        ).scalar_one()
        msg = conn.execute(
            text(
                "INSERT INTO messages (organization_id, channel_account_id, conversation_id, "
                "direction, provider_message_id, message_type, body_text, delivery_status, "
                "occurred_at, content_expires_at) VALUES (1, :ch, :cv, 'inbound', :pm, 'text', "
                "'hola', 'received', now(), now() + interval '30 days') RETURNING id"
            ),
            {"ch": channel, "cv": conv, "pm": f"wamid-0023-{n}"},
        ).scalar_one()
        ids.append((conv, msg))
    return ids[0][0], ids[0][1], ids[1][1]


def _b3_insert(conn, *, agent_key="reception", trigger="event", conversation_id=None,
               message_id=None):
    conn.execute(
        text(
            "INSERT INTO agent_runs (organization_id, agent_key, trigger, status, "
            "triggered_by_principal_id, conversation_id, trigger_message_id, finished_at) "
            "VALUES (1, :agent_key, :trigger, 'completed', 1, :conv, :msg, now())"
        ),
        {"agent_key": agent_key, "trigger": trigger, "conv": conversation_id, "msg": message_id},
    )


def test_migration_0023_round_trip_and_checks():
    from sqlalchemy.exc import IntegrityError

    url = _b2_disposable_url()
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, "0022")
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _cob_insert(conn, agent_key="reception")

        command.upgrade(config, "0023")
        with engine.begin() as conn:
            conv, msg, other_msg = _b3_seed_message(conn)
            _b3_insert(conn, conversation_id=conv, message_id=msg)
            _cob_insert(conn)  # COB rows keep both columns null
        bad_rows = (
            {},  # reception needs a conversation and a trigger message
            {"conversation_id": conv},
            {"trigger": "manual", "conversation_id": conv, "message_id": msg},
            {"agent_key": "inventario", "conversation_id": conv, "message_id": msg},
            {"conversation_id": conv, "message_id": other_msg},  # message of another conversation
            {"conversation_id": conv, "message_id": 999_999},
        )
        for bad in bad_rows:
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    _b3_insert(conn, **bad)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs"))

        command.downgrade(config, "0022")
        with engine.connect() as conn:
            columns = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_runs'"
                    )
                ).scalars()
            )
        assert not {"conversation_id", "trigger_message_id"} & columns
        command.upgrade(config, "0023")
    finally:
        engine.dispose()
        _b2_drop(url)


# --- SELF: migration 0024 (confirmaciones agent runs) on a disposable database ---


def test_migration_0024_round_trip_and_checks():
    from sqlalchemy.exc import IntegrityError

    url = _b2_disposable_url()
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, "0023")
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _cob_insert(conn, agent_key="confirmaciones")

        command.upgrade(config, "0024")
        with engine.begin() as conn:
            _cob_insert(conn, agent_key="confirmaciones")
            _cob_insert(conn)  # cobranza rows unchanged
        for bad in ({"agent_key": "inventario"}, {"agent_key": "reception"}):
            with pytest.raises(IntegrityError):  # reception still needs its trigger
                with engine.begin() as conn:
                    _cob_insert(conn, **bad)

        command.downgrade(config, "0023")
        with engine.begin() as conn:
            remaining = set(conn.execute(text("SELECT agent_key FROM agent_runs")).scalars())
        assert remaining == {"cobranza"}
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _cob_insert(conn, agent_key="confirmaciones")
        command.upgrade(config, "0024")
    finally:
        engine.dispose()
        _b2_drop(url)


# --- INV: migration 0025 (inventory agent kinds and runs) on a disposable database ---


def test_migration_0025_round_trip_and_checks():
    from sqlalchemy.exc import IntegrityError

    url = _b2_disposable_url()
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, "0024")
        for insert in (
            lambda conn: _cob_insert(conn, agent_key="inventario"),
            lambda conn: _b2_insert(conn, kind="inventory_transfer"),
            lambda conn: _b2_insert(conn, kind="inventory_entry"),
        ):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    insert(conn)

        command.upgrade(config, "0025")
        with engine.begin() as conn:
            _cob_insert(conn, agent_key="inventario")
            _cob_insert(conn)  # cobranza rows unchanged
            _b2_insert(conn, kind="inventory_transfer")
            _b2_insert(conn, kind="inventory_entry")
            _b2_insert(conn)  # collection kinds unchanged
        for bad in (
            lambda conn: _cob_insert(conn, agent_key="inventarios"),
            lambda conn: _b2_insert(conn, kind="inventory_adjustment"),
        ):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    bad(conn)

        command.downgrade(config, "0024")
        with engine.begin() as conn:
            runs = set(conn.execute(text("SELECT agent_key FROM agent_runs")).scalars())
            kinds = set(conn.execute(text("SELECT kind FROM agent_proposals")).scalars())
        assert runs == {"cobranza"} and kinds == {"collection_reminder"}
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _b2_insert(conn, kind="inventory_transfer")
        command.upgrade(config, "0025")
    finally:
        engine.dispose()
        _b2_drop(url)


# --- BACKFILL: migration 0026 (agent_jobs, waitlist_offer, backfill runs) ---


def _job_insert(conn, *, status="queued", agent_key="backfill", token=False, key=None):
    event_id = conn.execute(
        text(
            "INSERT INTO domain_events (organization_id, event_type, aggregate_type, "
            "aggregate_id, payload) VALUES (1, 'appointment.cancelled', 'appointment', '1', "
            "'{}'::jsonb) RETURNING id"
        )
    ).scalar()
    conn.execute(
        text(
            "INSERT INTO agent_jobs (organization_id, agent_key, job_key, source_event_id, "
            "run_after, status, lease_token, leased_until) VALUES (1, :agent, :key, :event, "
            "now(), :status, CASE WHEN :token THEN gen_random_uuid() END, "
            "CASE WHEN :token THEN now() END)"
        ),
        {"agent": agent_key, "key": key or f"backfill:event:{event_id}", "event": event_id,
         "status": status, "token": token},
    )


def test_migration_0026_round_trip_and_checks():
    from sqlalchemy.exc import IntegrityError

    url = _b2_disposable_url()
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, "0025")
        assert "agent_jobs" not in _b2_state(engine)[0]
        for insert in (
            lambda conn: _cob_insert(conn, agent_key="backfill"),
            lambda conn: _b2_insert(conn, kind="waitlist_offer"),
        ):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    insert(conn)

        command.upgrade(config, "0026")
        assert "agent_jobs" in _b2_state(engine)[0]
        with engine.begin() as conn:
            _cob_insert(conn, agent_key="backfill")
            _cob_insert(conn)  # cobranza rows unchanged
            _b2_insert(conn, kind="waitlist_offer")
            _b2_insert(conn, kind="inventory_entry")  # INV kinds unchanged
            _job_insert(conn)
            _job_insert(conn, status="leased", token=True)
            _job_insert(conn, key="dup")
        for bad in (
            lambda conn: _job_insert(conn, status="running"),
            lambda conn: _job_insert(conn, agent_key="cobranza"),
            lambda conn: _job_insert(conn, status="leased"),  # leased without a token
            lambda conn: _job_insert(conn, status="done", token=True),  # token without lease
            lambda conn: _job_insert(conn, key="dup"),  # one job per (org, job_key)
            lambda conn: _b2_insert(conn, kind="waitlist_offers"),
        ):
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    bad(conn)

        command.downgrade(config, "0025")
        assert "agent_jobs" not in _b2_state(engine)[0]
        with engine.begin() as conn:
            runs = set(conn.execute(text("SELECT agent_key FROM agent_runs")).scalars())
            kinds = set(conn.execute(text("SELECT kind FROM agent_proposals")).scalars())
        assert runs == {"cobranza"} and kinds == {"inventory_entry"}
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _b2_insert(conn, kind="waitlist_offer")
        command.upgrade(config, "0026")
    finally:
        engine.dispose()
        _b2_drop(url)
