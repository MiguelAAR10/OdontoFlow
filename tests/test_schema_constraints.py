import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.scheduling.models import Appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG


def _fixture_ids(session):
    with session.begin():
        session.execute(
            text(
                "INSERT INTO services (organization_id, name, duration_minutes) "
                f"VALUES ({ORG}, 'Limpieza', 30)"
            )
        )
        session.execute(
            text(
                "INSERT INTO locations (organization_id, name, timezone) "
                f"VALUES ({ORG}, 'Sede Centro', 'America/Lima')"
            )
        )
        session.execute(
            text("INSERT INTO practitioners (display_name) VALUES ('Dra. Ana')")
        )
        session.execute(
            text(
                "INSERT INTO leads (organization_id, full_name, contact_phone, acquisition_source) "
                f"VALUES ({ORG}, 'Juan', '+51999000111', 'direct')"
            )
        )
        service_id = session.execute(text("SELECT id FROM services")).scalar()
        location_id = session.execute(text("SELECT id FROM locations")).scalar()
        practitioner_id = session.execute(text("SELECT id FROM practitioners")).scalar()
        lead_id = session.execute(text("SELECT id FROM leads")).scalar()
        # A practitioner reaches a tenant's schedule only through a membership.
        session.execute(
            text(
                "INSERT INTO practitioner_memberships (organization_id, practitioner_id) "
                "VALUES (:org, :practitioner)"
            ),
            {"org": ORG, "practitioner": practitioner_id},
        )
    return {
        "organization_id": ORG,
        "service_id": service_id,
        "location_id": location_id,
        "practitioner_id": practitioner_id,
        "lead_id": lead_id,
    }


def test_fk_rejects_unknown_practitioner(session):
    ids = _fixture_ids(session)
    appointment = Appointment(
        organization_id=ids["organization_id"],
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
                    "INSERT INTO practitioner_capabilities (organization_id, practitioner_id, service_id, location_id) "
                    f"VALUES ({ORG}, 1, 999999, 1)"
                )
            )


def test_check_rejects_invalid_acquisition_source(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO leads (organization_id, full_name, contact_phone, acquisition_source) "
                    f"VALUES ({ORG}, 'X', '+51', 'walkin')"
                )
            )


def test_check_requires_contact_channel(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO leads (organization_id, full_name, acquisition_source) "
                    f"VALUES ({ORG}, 'X', 'direct')"
                )
            )


def test_check_rejects_non_positive_duration(session):
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO services (organization_id, name, duration_minutes) "
                    f"VALUES ({ORG}, 'X', 0)"
                )
            )


def test_check_rejects_invalid_appointment_state(session):
    ids = _fixture_ids(session)
    with pytest.raises(IntegrityError):
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO appointments (organization_id, lead_id, service_id, practitioner_id, "
                    "location_id, start_utc, end_utc, state) VALUES (:org, :l, :s, :p, :lo, "
                    "'2026-08-13T09:00:00+00:00', '2026-08-13T10:00:00+00:00', 'completed')"
                ),
                {
                    "org": ids["organization_id"],
                    "l": ids["lead_id"],
                    "s": ids["service_id"],
                    "p": ids["practitioner_id"],
                    "lo": ids["location_id"],
                },
            )
