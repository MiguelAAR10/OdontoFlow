"""B0.5 §5–§6 — domain_events atomicity and migration 0020 round trip."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from conftest import TEST_DATABASE_URL, _alembic_config
from app.audit.service import record_event
from app.context import default_context
from app.events.models import DomainEvent
from app.events.service import record_domain_event
from app.iam.permissions import (
    APPOINTMENTS_RECORD_OUTCOME,
    PAYMENTS_REVERSE,
    PERMISSION_CODES,
    REORDER_POINTS_MANAGE,
    WAITLIST_MANAGE,
    WAITLIST_READ,
)
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from test_domain_gaps_helpers import domain_events

NEW_CODES = (
    APPOINTMENTS_RECORD_OUTCOME,
    PAYMENTS_REVERSE,
    REORDER_POINTS_MANAGE,
    WAITLIST_READ,
    WAITLIST_MANAGE,
)
NEW_TABLES = {"domain_events", "payment_reversals", "reorder_points", "waitlist_entries"}


# --- domain_events ------------------------------------------------------------


def test_new_permission_codes_are_in_the_catalog():
    assert set(NEW_CODES) <= set(PERMISSION_CODES)
    assert set(NEW_CODES) == {
        "appointments.record_outcome",
        "payments.reverse",
        "reorder_points.manage",
        "waitlist.read",
        "waitlist.manage",
    }


def test_domain_event_is_present_after_commit(session):
    ctx = default_context(ORG)
    with session.begin():
        record_event(
            session, ctx=ctx, entity_type="probe", entity_id="1", action="probe.created"
        )
        event = record_domain_event(
            session,
            ctx=ctx,
            event_type="probe.created",
            aggregate_type="probe",
            aggregate_id="1",
            payload={"value": 1},
        )
        assert event.id is not None  # flushed, not committed

    rows = domain_events(session, "probe.created")
    assert len(rows) == 1
    assert rows[0].organization_id == ORG
    assert rows[0].payload == {"value": 1}
    assert rows[0].correlation_id == ctx.correlation_id
    assert rows[0].occurred_at is not None


def test_domain_event_is_absent_when_the_transaction_fails_later(session):
    ctx = default_context(ORG)
    with pytest.raises(RuntimeError):
        with session.begin():
            record_event(
                session, ctx=ctx, entity_type="probe", entity_id="2", action="probe.created"
            )
            record_domain_event(
                session,
                ctx=ctx,
                event_type="probe.failed",
                aggregate_type="probe",
                aggregate_id="2",
                payload={},
            )
            raise RuntimeError("fails after emitting")

    assert domain_events(session, "probe.failed") == []


def test_domain_event_is_absent_after_explicit_rollback(session):
    ctx = default_context(ORG)
    session.begin()
    record_domain_event(
        session,
        ctx=ctx,
        event_type="probe.rolled_back",
        aggregate_type="probe",
        aggregate_id="3",
        payload={},
    )
    session.rollback()

    assert domain_events(session, "probe.rolled_back") == []


def test_record_domain_event_never_commits(session):
    ctx = default_context(ORG)
    session.begin()
    record_domain_event(
        session, ctx=ctx, event_type="probe.open", aggregate_type="probe", aggregate_id="4", payload={}
    )
    assert session.in_transaction()
    session.rollback()
    assert domain_events(session, "probe.open") == []


def test_domain_event_organization_is_a_real_tenant(session):
    with pytest.raises(IntegrityError):
        session.add(
            DomainEvent(
                organization_id=999_999,
                event_type="probe.orphan",
                aggregate_type="probe",
                aggregate_id="5",
                payload={},
            )
        )
        session.flush()
    session.rollback()


# --- migration 0020 on a disposable database --------------------------------


@pytest.fixture
def disposable_url():
    name = f"odontoflow_test_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    with server.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    try:
        yield url
    finally:
        with server.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        server.dispose()


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        }


def _codes(engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT code FROM permissions WHERE code = ANY(:codes)"),
                {"codes": list(NEW_CODES)},
            ).scalars()
        )


def _system_grants(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM role_permissions rp "
                "JOIN roles r ON r.id = rp.role_id JOIN permissions p ON p.id = rp.permission_id "
                "WHERE r.code = 'system' AND p.code = ANY(:codes)"
            ),
            {"codes": list(NEW_CODES)},
        ).scalar_one()


def _insert_outcome_appointment(engine, state: str) -> None:
    with engine.begin() as conn:
        service = conn.execute(
            text(
                "INSERT INTO services (organization_id, name, duration_minutes) "
                "VALUES (1, 'Limpieza mig', 30) RETURNING id"
            )
        ).scalar_one()
        location = conn.execute(
            text(
                "INSERT INTO locations (organization_id, name, timezone) "
                "VALUES (1, 'Sede mig', 'America/Lima') RETURNING id"
            )
        ).scalar_one()
        practitioner = conn.execute(
            text("INSERT INTO practitioners (display_name) VALUES ('Dra. mig') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO practitioner_memberships (organization_id, practitioner_id) "
                "VALUES (1, :p)"
            ),
            {"p": practitioner},
        )
        lead = conn.execute(
            text(
                "INSERT INTO leads (organization_id, full_name, contact_phone, acquisition_source) "
                "VALUES (1, 'Juan mig', '+51999000999', 'direct') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO appointments (organization_id, lead_id, service_id, practitioner_id, "
                "location_id, start_utc, end_utc, state) VALUES (1, :lead, :service, :p, :loc, "
                "'2026-08-10T14:00:00+00', '2026-08-10T14:30:00+00', :state)"
            ),
            {"lead": lead, "service": service, "p": practitioner, "loc": location, "state": state},
        )


def test_migration_0020_upgrade_downgrade_upgrade(disposable_url):
    config = _alembic_config(disposable_url)
    command.upgrade(config, "0019")
    engine = create_engine(disposable_url)
    try:
        before = _tables(engine)
        assert NEW_TABLES.isdisjoint(before)

        command.upgrade(config, "0020")
        assert NEW_TABLES <= _tables(engine)
        assert _codes(engine) == set(NEW_CODES)
        assert _system_grants(engine) == len(NEW_CODES)
        _insert_outcome_appointment(engine, "completed")  # the widened CHECK accepts it
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM appointments"))

        command.downgrade(config, "0019")
        assert _tables(engine) == before
        assert _codes(engine) == set()
        with pytest.raises(IntegrityError):
            _insert_outcome_appointment(engine, "no_show")  # original CHECK restored

        command.upgrade(config, "0020")
        assert NEW_TABLES <= _tables(engine)
        assert _system_grants(engine) == len(NEW_CODES)
    finally:
        engine.dispose()


def test_downgrade_refuses_while_outcome_states_exist(disposable_url):
    config = _alembic_config(disposable_url)
    command.upgrade(config, "0020")
    engine = create_engine(disposable_url)
    try:
        _insert_outcome_appointment(engine, "no_show")

        with pytest.raises(DBAPIError) as raised:
            command.downgrade(config, "0019")
        assert "completed/no_show" in str(raised.value)

        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            state = conn.execute(text("SELECT state FROM appointments")).scalar_one()
        assert version == "0020"
        assert state == "no_show"
        assert NEW_TABLES <= _tables(engine)
    finally:
        engine.dispose()


def _insert_reversed_payment(engine) -> None:
    """One full-chain payment plus its reversal, inserted with plain SQL."""
    with engine.begin() as conn:
        service = conn.execute(
            text(
                "INSERT INTO services (organization_id, name, duration_minutes) "
                "VALUES (1, 'Limpieza rev', 30) RETURNING id"
            )
        ).scalar_one()
        location = conn.execute(
            text(
                "INSERT INTO locations (organization_id, name, timezone) "
                "VALUES (1, 'Sede rev', 'America/Lima') RETURNING id"
            )
        ).scalar_one()
        practitioner = conn.execute(
            text("INSERT INTO practitioners (display_name) VALUES ('Dra. rev') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO practitioner_memberships (organization_id, practitioner_id) "
                "VALUES (1, :p)"
            ),
            {"p": practitioner},
        )
        patient = conn.execute(
            text("INSERT INTO patients (organization_id, full_name) VALUES (1, 'Ana rev') RETURNING id")
        ).scalar_one()
        visit = conn.execute(
            text(
                "INSERT INTO visits (organization_id, patient_id, practitioner_id, location_id) "
                "VALUES (1, :patient, :p, :loc) RETURNING id"
            ),
            {"patient": patient, "p": practitioner, "loc": location},
        ).scalar_one()
        execution = conn.execute(
            text(
                "INSERT INTO service_executions (organization_id, visit_id, service_id, executed_price) "
                "VALUES (1, :visit, :service, 150) RETURNING id"
            ),
            {"visit": visit, "service": service},
        ).scalar_one()
        charge = conn.execute(
            text(
                "INSERT INTO charges (organization_id, service_execution_id, amount) "
                "VALUES (1, :execution, 150) RETURNING id"
            ),
            {"execution": execution},
        ).scalar_one()
        payment = conn.execute(
            text(
                "INSERT INTO payments (organization_id, charge_id, amount, method) "
                "VALUES (1, :charge, 150, 'efectivo') RETURNING id"
            ),
            {"charge": charge},
        ).scalar_one()
        principal = conn.execute(
            text(
                "INSERT INTO principals (type, display_name) VALUES ('human', 'Caja rev') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO payment_reversals "
                "(organization_id, payment_id, reason, created_by_principal_id) "
                "VALUES (1, :payment, 'Error de caja', :principal)"
            ),
            {"payment": payment, "principal": principal},
        )


def test_downgrade_refuses_while_payment_reversals_exist(disposable_url):
    """Dropping payment_reversals would silently turn reversed payments back into paid charges."""
    config = _alembic_config(disposable_url)
    command.upgrade(config, "0020")
    engine = create_engine(disposable_url)
    try:
        _insert_reversed_payment(engine)

        with pytest.raises(DBAPIError) as raised:
            command.downgrade(config, "0019")
        assert "payment_reversals" in str(raised.value)

        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            reversals = conn.execute(text("SELECT count(*) FROM payment_reversals")).scalar_one()
        assert version == "0020"
        assert reversals == 1
        assert NEW_TABLES <= _tables(engine)
    finally:
        engine.dispose()
