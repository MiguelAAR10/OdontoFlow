import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.scheduling.models import Appointment


def _fixture_ids(session):
    with session.begin():
        session.execute(
            text(
                "INSERT INTO services (name, duration_minutes) VALUES ('Limpieza', 30)"
            )
        )
        session.execute(
            text("INSERT INTO locations (name, timezone) VALUES ('Sede Centro', 'America/Lima')")
        )
        session.execute(
            text("INSERT INTO practitioners (display_name) VALUES ('Dra. Ana')")
        )
        session.execute(
            text("INSERT INTO leads (full_name, contact_phone, acquisition_source) VALUES ('Juan', '+51999000111', 'direct')")
        )
        service_id = session.execute(text("SELECT id FROM services")).scalar()
        location_id = session.execute(text("SELECT id FROM locations")).scalar()
        practitioner_id = session.execute(text("SELECT id FROM practitioners")).scalar()
        lead_id = session.execute(text("SELECT id FROM leads")).scalar()
    return {
        "service_id": service_id,
        "location_id": location_id,
        "practitioner_id": practitioner_id,
        "lead_id": lead_id,
    }


def test_fk_rejects_unknown_practitioner(session):
    ids = _fixture_ids(session)
    appointment = Appointment(
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        practitioner_id=999999,
        location_id=ids["location_id"],
        start_utc="2026-08-13T09:00:00+00:00",
        end_utc="2026-08-13T10:00:00+00:00",
        state="confirmed",
    )
    session.add(appointment)
    with pytest.raises(IntegrityError):
        session.commit()


def test_fk_rejects_unknown_service_capability(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO practitioner_capabilities (practitioner_id, service_id, location_id) "
                    "VALUES (1, 999999, 1)"
                )
            )


def test_check_rejects_invalid_acquisition_source(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO leads (full_name, contact_phone, acquisition_source) "
                    "VALUES ('X', '+51', 'walkin')"
                )
            )


def test_check_requires_contact_channel(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO leads (full_name, acquisition_source) "
                    "VALUES ('X', 'direct')"
                )
            )


def test_check_rejects_non_positive_duration(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text("INSERT INTO services (name, duration_minutes) VALUES ('X', 0)")
            )


def test_check_rejects_invalid_appointment_state(session):
    ids = _fixture_ids(session)
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO appointments (lead_id, service_id, practitioner_id, location_id, "
                    "start_utc, end_utc, state) VALUES (:l, :s, :p, :lo, "
                    "'2026-08-13T09:00:00+00:00', '2026-08-13T10:00:00+00:00', 'completed')"
                ),
                {
                    "l": ids["lead_id"],
                    "s": ids["service_id"],
                    "p": ids["practitioner_id"],
                    "lo": ids["location_id"],
                },
            )
