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


def _appointment(ids, start, end, state="confirmed") -> Appointment:
    return Appointment(
        organization_id=ids["organization_id"],
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"],
        start_utc=start,
        end_utc=end,
        state=state,
    )


def test_non_overlapping_confirmed_appointments_allowed(session):
    ids = _fixture_ids(session)
    session.add(_appointment(ids, "2026-08-13T09:00:00+00:00", "2026-08-13T10:00:00+00:00"))
    session.add(_appointment(ids, "2026-08-13T10:00:00+00:00", "2026-08-13T11:00:00+00:00"))
    session.commit()
    count = session.execute(text("SELECT count(*) FROM appointments")).scalar()
    assert count == 2


def test_overlapping_confirmed_appointments_rejected(session):
    ids = _fixture_ids(session)
    session.add(_appointment(ids, "2026-08-13T09:00:00+00:00", "2026-08-13T10:00:00+00:00"))
    session.commit()
    session.add(_appointment(ids, "2026-08-13T09:30:00+00:00", "2026-08-13T10:30:00+00:00"))
    with pytest.raises(IntegrityError) as exc:
        session.commit()
    assert exc.value.orig.sqlstate == "23P01"


def test_cancelled_appointment_does_not_block_interval_reuse(session):
    ids = _fixture_ids(session)
    first = _appointment(ids, "2026-08-13T09:00:00+00:00", "2026-08-13T10:00:00+00:00")
    session.add(first)
    session.commit()
    first.state = "cancelled"
    session.commit()

    second = _appointment(ids, "2026-08-13T09:00:00+00:00", "2026-08-13T10:00:00+00:00")
    session.add(second)
    session.commit()
    count = session.execute(
        text("SELECT count(*) FROM appointments WHERE state = 'confirmed'")
    ).scalar()
    assert count == 1


def test_cancelled_appointment_may_overlap_confirmed(session):
    ids = _fixture_ids(session)
    session.add(_appointment(ids, "2026-08-13T09:00:00+00:00", "2026-08-13T10:00:00+00:00"))
    session.commit()
    cancelled = _appointment(ids, "2026-08-13T09:30:00+00:00", "2026-08-13T10:00:00+00:00", state="cancelled")
    session.add(cancelled)
    session.commit()
    count = session.execute(text("SELECT count(*) FROM appointments")).scalar()
    assert count == 2
