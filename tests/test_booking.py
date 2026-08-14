"""Task 7 — booking transaction integration tests.

Every test exercises the authoritative use case ``book_appointment`` against
real PostgreSQL: the preflight validation, the catalog-owned duration, the
Task 6 availability engine, the audit row written in the same transaction and
the GiST exclusion constraint as the final concurrency authority.
"""

import threading
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.errors import AppError, ErrorCode
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock
from app.scheduling.service import book_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)  # weekday 0, same anchor day used by the Task 6 tests


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed(
    session,
    *,
    duration_minutes=30,
    service_active=True,
    location_active=True,
    practitioner_active=True,
    capability=True,
    capability_active=True,
    rule_window=(time(9, 0), time(13, 0)),
):
    service = Service(
        organization_id=ORG,
        name="Limpieza dental",
        duration_minutes=duration_minutes,
        is_active=service_active,
    )
    location = Location(
        organization_id=ORG, name="Sede Centro", timezone=LIMA, is_active=location_active
    )
    practitioner = Practitioner(display_name="Dra. Ana", is_active=practitioner_active)
    lead = Lead(
        organization_id=ORG,
        full_name="Juan Pérez",
        contact_phone="+51999000111",
        acquisition_source="direct",
    )
    session.add_all([service, location, practitioner, lead])
    session.flush()
    # The global practitioner identity reaches this tenant's schedule only
    # through its membership row (PF0 PM2).
    session.add(
        PractitionerMembership(
            organization_id=ORG, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.flush()
    if capability:
        session.add(
            PractitionerCapability(
                organization_id=ORG,
                practitioner_id=practitioner.id,
                service_id=service.id,
                location_id=location.id,
                is_active=capability_active,
            )
        )
    if rule_window is not None:
        session.add(
            AvailabilityRule(
                organization_id=ORG,
                practitioner_id=practitioner.id,
                location_id=location.id,
                day_of_week=0,
                start_local=rule_window[0],
                end_local=rule_window[1],
            )
        )
    session.commit()
    return {
        "organization_id": ORG,
        "lead_id": lead.id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
    }


def book(session, ids, start=None, **overrides):
    payload = dict(ids)
    payload["start"] = start if start is not None else local(9)
    payload.update(overrides)
    return book_appointment(session, **payload)


def add_appointment(session, ids, start_utc, end_utc, state="confirmed"):
    appointment = Appointment(
        organization_id=ids["organization_id"],
        lead_id=ids["lead_id"],
        service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"],
        start_utc=start_utc,
        end_utc=end_utc,
        state=state,
    )
    session.add(appointment)
    session.commit()
    return appointment


def persisted_appointments(session):
    rows = session.execute(
        text("SELECT id, start_utc, end_utc, state FROM appointments ORDER BY id")
    ).all()
    session.rollback()
    return rows


def count_of(session, model):
    total = session.scalar(select(func.count()).select_from(model))
    session.rollback()
    return total


# --- 1. valid booking ------------------------------------------------------


def test_valid_booking_persists_one_confirmed_appointment(session):
    ids = seed(session)

    appointment = book(session, ids)

    assert appointment.id is not None
    assert appointment.state == "confirmed"
    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].start_utc == utc_of(9)
    assert rows[0].end_utc == utc_of(9, 30)
    assert rows[0].state == "confirmed"


def test_booking_persists_lead_service_practitioner_and_location(session):
    ids = seed(session)

    appointment = book(session, ids)

    stored = session.get(Appointment, appointment.id)
    assert stored.lead_id == ids["lead_id"]
    assert stored.service_id == ids["service_id"]
    assert stored.practitioner_id == ids["practitioner_id"]
    assert stored.location_id == ids["location_id"]
    session.rollback()


# --- 2. authoritative duration --------------------------------------------


def test_duration_comes_from_catalog_and_caller_cannot_override(session):
    ids = seed(session, duration_minutes=45)

    with pytest.raises(TypeError):
        book(session, ids, duration_minutes=15)
    with pytest.raises(TypeError):
        book(session, ids, end=local(9, 15))

    appointment = book(session, ids, start=local(9))

    assert appointment.end_utc == utc_of(9, 45)
    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].end_utc - rows[0].start_utc == timedelta(minutes=45)
    assert rows[0].end_utc == utc_of(9, 45)


# --- 3-6. existence and active-state revalidation --------------------------


def test_missing_lead_raises_not_found(session):
    ids = seed(session)

    with pytest.raises(AppError) as exc:
        book(session, ids, lead_id=999_999)

    assert exc.value.code is ErrorCode.NOT_FOUND
    assert count_of(session, Appointment) == 0


@pytest.mark.parametrize("field", ["service_id", "location_id", "practitioner_id"])
def test_missing_referenced_entity_raises_not_found(session, field):
    ids = seed(session)

    with pytest.raises(AppError) as exc:
        book(session, ids, **{field: 999_999})

    assert exc.value.code is ErrorCode.NOT_FOUND
    assert count_of(session, Appointment) == 0


def test_inactive_service_raises_entity_inactive(session):
    ids = seed(session, service_active=False)

    with pytest.raises(AppError) as exc:
        book(session, ids)

    assert exc.value.code is ErrorCode.ENTITY_INACTIVE
    assert count_of(session, Appointment) == 0


def test_inactive_location_raises_entity_inactive(session):
    ids = seed(session, location_active=False)

    with pytest.raises(AppError) as exc:
        book(session, ids)

    assert exc.value.code is ErrorCode.ENTITY_INACTIVE
    assert count_of(session, Appointment) == 0


def test_inactive_practitioner_raises_entity_inactive(session):
    ids = seed(session, practitioner_active=False)

    with pytest.raises(AppError) as exc:
        book(session, ids)

    assert exc.value.code is ErrorCode.ENTITY_INACTIVE
    assert count_of(session, Appointment) == 0


# --- 7-8. capability -------------------------------------------------------


def test_missing_capability_raises_capability_missing(session):
    ids = seed(session, capability=False)

    with pytest.raises(AppError) as exc:
        book(session, ids)

    assert exc.value.code is ErrorCode.CAPABILITY_MISSING
    assert count_of(session, Appointment) == 0


def test_inactive_capability_raises_capability_missing(session):
    ids = seed(session, capability_active=False)

    with pytest.raises(AppError) as exc:
        book(session, ids)

    assert exc.value.code is ErrorCode.CAPABILITY_MISSING
    assert count_of(session, Appointment) == 0


# --- 9-11. availability preflight ------------------------------------------


def test_start_outside_recurring_availability_raises_slot_blocked(session):
    ids = seed(session, rule_window=(time(9, 0), time(13, 0)))

    with pytest.raises(AppError) as exc:
        book(session, ids, start=local(15))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert count_of(session, Appointment) == 0


def test_start_off_the_fifteen_minute_grid_raises_slot_blocked(session):
    ids = seed(session)

    with pytest.raises(AppError) as exc:
        book(session, ids, start=local(9, 7))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert count_of(session, Appointment) == 0


def test_interval_extending_past_availability_end_raises_slot_blocked(session):
    ids = seed(session, duration_minutes=45, rule_window=(time(9, 0), time(10, 0)))

    with pytest.raises(AppError) as exc:
        book(session, ids, start=local(9, 30))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert count_of(session, Appointment) == 0


def test_start_intersecting_schedule_block_raises_slot_blocked(session):
    ids = seed(session)
    session.add(
        ScheduleBlock(
            organization_id=ids["organization_id"],
            practitioner_id=ids["practitioner_id"],
            location_id=ids["location_id"],
            start_utc=utc_of(9, 15),
            end_utc=utc_of(10),
        )
    )
    session.commit()

    with pytest.raises(AppError) as exc:
        book(session, ids, start=local(9))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert count_of(session, Appointment) == 0


def test_collision_with_confirmed_appointment_is_rejected_by_preflight(session):
    ids = seed(session)
    add_appointment(session, ids, utc_of(9), utc_of(9, 30))

    with pytest.raises(AppError) as exc:
        book(session, ids, start=local(9))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    rows = persisted_appointments(session)
    assert len(rows) == 1


def test_partial_overlap_with_confirmed_appointment_is_rejected_by_preflight(session):
    ids = seed(session)
    add_appointment(session, ids, utc_of(9), utc_of(9, 30))

    with pytest.raises(AppError) as exc:
        book(session, ids, start=local(9, 15))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert len(persisted_appointments(session)) == 1


# --- 12-13. cancelled rows and half-open boundaries ------------------------


def test_cancelled_appointment_does_not_block_rebooking_same_interval(session):
    ids = seed(session)
    add_appointment(session, ids, utc_of(9), utc_of(9, 30), state="cancelled")

    appointment = book(session, ids, start=local(9))

    assert appointment.state == "confirmed"
    assert appointment.start_utc == utc_of(9)
    confirmed = session.execute(
        text("SELECT count(*) FROM appointments WHERE state = 'confirmed'")
    ).scalar()
    session.rollback()
    assert confirmed == 1


def test_back_to_back_booking_on_half_open_boundary_succeeds(session):
    ids = seed(session)
    add_appointment(session, ids, utc_of(9), utc_of(9, 30))

    appointment = book(session, ids, start=local(9, 30))

    assert appointment.start_utc == utc_of(9, 30)
    assert appointment.end_utc == utc_of(10)
    rows = persisted_appointments(session)
    assert len(rows) == 2


# --- time contract ---------------------------------------------------------


def test_naive_start_is_rejected_as_invalid_input(session):
    ids = seed(session)

    with pytest.raises(AppError) as exc:
        book(session, ids, start=datetime(2026, 8, 10, 9, 0))

    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert count_of(session, Appointment) == 0


def test_equivalent_instant_in_another_zone_books_the_same_utc_interval(session):
    ids = seed(session)

    appointment = book(session, ids, start=utc_of(9))  # 14:00Z == 09:00 Lima

    assert appointment.start_utc == utc_of(9)
    assert appointment.end_utc == utc_of(9, 30)
    assert len(persisted_appointments(session)) == 1


# --- 14-15. audit atomicity ------------------------------------------------


def test_successful_booking_writes_exactly_one_creation_audit_event(session):
    ids = seed(session)

    appointment = book(session, ids, correlation_id="corr-123")

    events = list(session.scalars(select(AuditEvent)))
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "appointment"
    assert event.action == "appointment.created"
    assert event.entity_id == str(appointment.id)
    assert event.correlation_id == "corr-123"
    assert event.before_state is None
    assert event.after_state["id"] == appointment.id
    assert event.after_state["state"] == "confirmed"
    assert event.after_state["start_utc"] == utc_of(9).isoformat()
    assert event.after_state["end_utc"] == utc_of(9, 30).isoformat()
    assert event.occurred_at is not None
    session.rollback()


def test_audit_event_records_supplied_actor(session):
    ids = seed(session)

    book(session, ids, actor_id="recepcion-01", actor_type="staff")

    event = session.scalars(select(AuditEvent)).one()
    assert event.actor_id == "recepcion-01"
    assert event.actor_type == "staff"
    session.rollback()


def test_failed_booking_writes_neither_appointment_nor_audit_event(session):
    ids = seed(session)

    with pytest.raises(AppError):
        book(session, ids, start=local(15))

    assert count_of(session, Appointment) == 0
    assert count_of(session, AuditEvent) == 0


# --- 16-17. real concurrency and session recovery --------------------------


BLOCKED_INSERTS = text(
    """
    SELECT count(*) FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    WHERE c.relname = 'appointments' AND NOT l.granted
    """
)


def _await_blocked_inserts(session, expected, timeout=30.0):
    """Poll (never sleep) until ``expected`` backends are blocked on INSERT."""
    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        blocked = session.execute(BLOCKED_INSERTS).scalar()
        session.rollback()
        if blocked >= expected:
            return True
    return False


def test_concurrent_bookings_of_the_same_slot_persist_exactly_one(migrated_engine, session):
    ids = seed(session)
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    # Two threads alone do not reliably interleave: the ORM read phase is
    # GIL-bound, so one transaction can finish before the other has read
    # anything, and the loser would fail the preflight instead of the
    # constraint. An EXCLUSIVE table lock pins the interleaving we must prove
    # safe: plain SELECTs still pass, so both threads complete their preflight
    # and see a free slot, while both INSERTs block until the lock is released.
    # From there the GiST exclusion alone decides the winner.
    locker = maker()
    locker.execute(text("LOCK TABLE appointments IN EXCLUSIVE MODE"))

    def attempt():
        db = maker()
        # Warm the pooled connection so the barrier — not connection setup —
        # decides when each thread reaches the booking race.
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            appointment = book(db, ids, start=local(9))
            with guard:
                outcomes.append(("committed", appointment.id))
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            db.rollback()
            with guard:
                outcomes.append(("failed", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        both_passed_preflight = _await_blocked_inserts(session, 2)
    finally:
        locker.commit()  # release the gate; the constraint takes over
        locker.close()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert both_passed_preflight, outcomes

    committed = [result for result in outcomes if result[0] == "committed"]
    failed = [result for result in outcomes if result[0] == "failed"]
    assert len(committed) == 1, outcomes
    assert len(failed) == 1, outcomes

    winner_id = committed[0][1]
    loser = failed[0][1]
    # Both inserts are released at the same instant, so PostgreSQL settles the
    # race one of two ways: the loser checks the exclusion constraint after the
    # winner committed (23P01), or both check at once, each waiting on the
    # other's transaction, and the deadlock detector picks a victim (40P01).
    # Both are the constraint doing its job; exactly one row survives either
    # way. The deterministic 23P01 path is asserted in the next test.
    assert isinstance(loser, (IntegrityError, OperationalError)), repr(loser)
    assert loser.orig.sqlstate in {"23P01", "40P01"}, repr(loser)

    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].id == winner_id
    assert rows[0].state == "confirmed"
    assert rows[0].start_utc == utc_of(9)
    assert count_of(session, AuditEvent) == 1


def test_booking_defeated_by_a_committed_row_propagates_sqlstate_23P01(
    migrated_engine, session
):
    """The exclusion constraint — not the preflight — rejects the late booker.

    The booking thread is gated at its INSERT *after* a preflight that saw a
    free slot; the winning row is then committed underneath it. The service
    must let the resulting ``IntegrityError`` escape untouched so the transport
    layer can map SQLSTATE 23P01 to the stable appointment-conflict response.
    """
    ids = seed(session)
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    outcome = []

    gate = maker()
    gate.execute(text("LOCK TABLE appointments IN EXCLUSIVE MODE"))

    def attempt():
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            appointment = book(db, ids, start=local(9))
            outcome.append(("committed", appointment.id))
        except Exception as exc:  # noqa: BLE001 - the raised error is the assertion
            db.rollback()
            outcome.append(("failed", exc))
        finally:
            db.close()

    thread = threading.Thread(target=attempt)
    try:
        thread.start()
        # Blocking on the INSERT proves the preflight already ran and found the
        # slot free; only then does the winning row appear underneath it.
        blocked_after_preflight = _await_blocked_inserts(session, 1)
        gate.execute(
            text(
                "INSERT INTO appointments"
                " (organization_id, lead_id, service_id, practitioner_id, location_id,"
                "  start_utc, end_utc, state)"
                " VALUES (:org, :lead, :service, :practitioner, :location,"
                "         :start, :end, 'confirmed')"
            ),
            {
                "org": ids["organization_id"],
                "lead": ids["lead_id"],
                "service": ids["service_id"],
                "practitioner": ids["practitioner_id"],
                "location": ids["location_id"],
                "start": utc_of(9),
                "end": utc_of(9, 30),
            },
        )
    finally:
        gate.commit()
        gate.close()
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert blocked_after_preflight, outcome
    assert len(outcome) == 1 and outcome[0][0] == "failed", outcome
    loser = outcome[0][1]
    assert isinstance(loser, IntegrityError), repr(loser)
    assert loser.orig.sqlstate == "23P01"

    rows = persisted_appointments(session)
    assert len(rows) == 1  # only the row committed underneath the booker
    assert count_of(session, AuditEvent) == 0  # no orphan audit row


def test_session_is_reusable_after_exclusion_conflict_rollback(migrated_engine, session):
    ids = seed(session)
    other = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)()
    try:
        book(other, ids, start=local(9))

        # Insert the same interval bypassing the preflight so the GiST — not the
        # application — rejects it, then prove the session still works.
        conflicting = Appointment(
            organization_id=ids["organization_id"],
            lead_id=ids["lead_id"],
            service_id=ids["service_id"],
            practitioner_id=ids["practitioner_id"],
            location_id=ids["location_id"],
            start_utc=utc_of(9),
            end_utc=utc_of(9, 30),
            state="confirmed",
        )
        session.add(conflicting)
        with pytest.raises(IntegrityError) as exc:
            session.commit()
        assert exc.value.orig.sqlstate == "23P01"
        session.rollback()

        assert session.scalar(select(func.count()).select_from(Appointment)) == 1
        session.commit()

        later = book(session, ids, start=local(9, 30))
        assert later.id is not None
        assert len(persisted_appointments(session)) == 2
    finally:
        other.close()
