"""Task 9 — rescheduling use case integration tests.

``reschedule_appointment`` moves one existing row: same appointment id, no
temporary cancellation, no intermediate two-record transition. The tests cover
the row lock, the self-exclusion during slot revalidation, the catalog-owned
duration, the single audit record carrying both intervals, and the GiST
exclusion constraint as the final concurrency authority — plus the two races
(reschedule vs reschedule, cancel vs reschedule) that the row lock must
serialize.
"""

import threading
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock
from app.scheduling.service import (
    book_appointment,
    cancel_appointment,
    reschedule_appointment,
)
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
TZ = ZoneInfo(LIMA)
UTC = timezone.utc
MONDAY = date(2026, 8, 10)  # weekday 0, the anchor day used since Task 6


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed(session, *, duration_minutes=30, rule_window=(time(9, 0), time(13, 0))):
    service = Service(
        organization_id=ORG,
        name="Limpieza dental",
        duration_minutes=duration_minutes,
        is_active=True,
    )
    location = Location(
        organization_id=ORG, name="Sede Centro", timezone=LIMA, is_active=True
    )
    practitioner = Practitioner(display_name="Dra. Ana", is_active=True)
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
    session.add(
        PractitionerCapability(
            organization_id=ORG,
            practitioner_id=practitioner.id,
            service_id=service.id,
            location_id=location.id,
            is_active=True,
        )
    )
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


def audit_events(session, appointment_id=None):
    query = select(AuditEvent).order_by(AuditEvent.id)
    if appointment_id is not None:
        query = query.where(AuditEvent.entity_id == str(appointment_id))
    events = [
        {
            "action": event.action,
            "entity_id": event.entity_id,
            "before_state": event.before_state,
            "after_state": event.after_state,
            "actor_id": event.actor_id,
            "actor_type": event.actor_type,
            "correlation_id": event.correlation_id,
        }
        for event in session.scalars(query)
    ]
    session.rollback()
    return events


def reschedule_actions(session):
    return [
        event for event in audit_events(session)
        if event["action"] == "appointment.rescheduled"
    ]


# --- 8-9. the row moves, the duration stays authoritative -------------------


def test_reschedule_updates_the_same_appointment_row(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    moved = reschedule_appointment(session, appointment_id, local(10))

    assert moved.id == appointment_id
    assert moved.state == "confirmed"
    assert moved.start_utc == utc_of(10)
    rows = persisted_appointments(session)
    assert len(rows) == 1  # no new row, no cancelled twin
    assert rows[0].id == appointment_id
    assert rows[0].start_utc == utc_of(10)
    assert rows[0].state == "confirmed"


def test_new_end_uses_the_canonical_service_duration(session):
    ids = seed(session, duration_minutes=45)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    moved = reschedule_appointment(session, appointment_id, local(10))

    assert moved.start_utc == utc_of(10)
    assert moved.end_utc == utc_of(10, 45)
    rows = persisted_appointments(session)
    assert rows[0].end_utc - rows[0].start_utc == timedelta(minutes=45)


def test_caller_cannot_supply_duration_or_end_to_the_use_case(session):
    ids = seed(session, duration_minutes=45)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    with pytest.raises(TypeError):
        reschedule_appointment(session, appointment_id, local(10), duration_minutes=15)
    with pytest.raises(TypeError):
        reschedule_appointment(session, appointment_id, local(10), end=local(10, 15))
    with pytest.raises(TypeError):
        reschedule_appointment(session, appointment_id, local(10), state="cancelled")

    rows = persisted_appointments(session)
    assert rows[0].start_utc == utc_of(9)


def test_naive_new_start_is_rejected_as_invalid_input(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, datetime(2026, 8, 10, 10, 0))

    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.http_status == 422
    rows = persisted_appointments(session)
    assert rows[0].start_utc == utc_of(9)


# --- 11. the appointment does not block itself ------------------------------


def test_appointment_does_not_block_itself_during_revalidation(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    # The new interval overlaps the current one: only self-exclusion makes it
    # bookable, and only the GiST's own-row handling makes the UPDATE legal.
    moved = reschedule_appointment(session, appointment_id, local(9, 15))

    assert moved.id == appointment_id
    assert moved.start_utc == utc_of(9, 15)
    assert moved.end_utc == utc_of(9, 45)
    rows = persisted_appointments(session)
    assert len(rows) == 1


def test_rescheduling_to_the_same_interval_is_accepted(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    moved = reschedule_appointment(session, appointment_id, local(9))

    assert moved.start_utc == utc_of(9)
    assert moved.end_utc == utc_of(9, 30)
    assert len(persisted_appointments(session)) == 1


# --- 12-15. availability revalidation ---------------------------------------


def test_reschedule_outside_recurring_availability_raises_slot_blocked(session):
    ids = seed(session, rule_window=(time(9, 0), time(13, 0)))
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(15))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    rows = persisted_appointments(session)
    assert rows[0].start_utc == utc_of(9)
    assert rows[0].state == "confirmed"


def test_reschedule_off_the_fifteen_minute_grid_raises_slot_blocked(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10, 7))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert persisted_appointments(session)[0].start_utc == utc_of(9)


def test_reschedule_into_a_schedule_block_raises_slot_blocked(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    session.add(
        ScheduleBlock(
            organization_id=ids["organization_id"],
            practitioner_id=ids["practitioner_id"],
            location_id=ids["location_id"],
            start_utc=utc_of(10),
            end_utc=utc_of(11),
        )
    )
    session.commit()

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10, 15))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert persisted_appointments(session)[0].start_utc == utc_of(9)


def test_another_confirmed_appointment_blocks_the_new_interval(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    other = book(session, ids, start=local(10))

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    rows = persisted_appointments(session)
    assert len(rows) == 2
    assert {row.start_utc for row in rows} == {utc_of(9), utc_of(10)}
    assert other.start_utc == utc_of(10)


def test_partial_overlap_with_another_confirmed_appointment_is_blocked(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    book(session, ids, start=local(10))

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10, 15))

    assert exc.value.code is ErrorCode.SLOT_BLOCKED
    assert persisted_appointments(session)[0].start_utc == utc_of(9)


def test_cancelled_appointment_does_not_block_the_new_interval(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    add_appointment(session, ids, utc_of(10), utc_of(10, 30), state="cancelled")

    moved = reschedule_appointment(session, appointment_id, local(10))

    assert moved.id == appointment_id
    assert moved.start_utc == utc_of(10)
    confirmed = [
        row for row in persisted_appointments(session) if row.state == "confirmed"
    ]
    assert len(confirmed) == 1
    assert confirmed[0].start_utc == utc_of(10)


# --- 16-18. entity and capability revalidation ------------------------------


def test_missing_appointment_raises_not_found(session):
    seed(session)

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, 999_999, local(10))

    assert exc.value.code is ErrorCode.NOT_FOUND
    assert exc.value.http_status == 404


def test_rescheduling_a_cancelled_appointment_raises_a_stable_conflict(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    cancel_appointment(session, appointment_id)

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10))

    assert exc.value.code is ErrorCode.ENTITY_INACTIVE
    assert exc.value.http_status == 409
    rows = persisted_appointments(session)
    assert rows[0].state == "cancelled"
    assert rows[0].start_utc == utc_of(9)  # the interval is still preserved
    assert reschedule_actions(session) == []


@pytest.mark.parametrize(
    "model, key",
    [(Service, "service_id"), (Location, "location_id"), (Practitioner, "practitioner_id")],
)
def test_inactive_entity_raises_entity_inactive(session, model, key):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    entity = session.get(model, ids[key])
    entity.is_active = False
    session.commit()

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10))

    assert exc.value.code is ErrorCode.ENTITY_INACTIVE
    rows = persisted_appointments(session)
    assert rows[0].start_utc == utc_of(9)
    assert reschedule_actions(session) == []


def test_missing_capability_raises_capability_missing(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    session.execute(text("DELETE FROM practitioner_capabilities"))
    session.commit()

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10))

    assert exc.value.code is ErrorCode.CAPABILITY_MISSING
    assert persisted_appointments(session)[0].start_utc == utc_of(9)


def test_inactive_capability_raises_capability_missing(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    capability = session.scalars(select(PractitionerCapability)).one()
    capability.is_active = False
    session.commit()

    with pytest.raises(AppError) as exc:
        reschedule_appointment(session, appointment_id, local(10))

    assert exc.value.code is ErrorCode.CAPABILITY_MISSING
    assert persisted_appointments(session)[0].start_utc == utc_of(9)
    assert reschedule_actions(session) == []


# --- 19-22. audit and atomicity ---------------------------------------------


def test_successful_reschedule_writes_exactly_one_rescheduled_audit_event(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    reschedule_appointment(
        session,
        appointment_id,
        local(10),
        actor_id="recepcion-01",
        actor_type="staff",
        correlation_id="corr-resched-1",
    )

    events = reschedule_actions(session)
    assert len(events) == 1
    event = events[0]
    assert event["entity_id"] == str(appointment_id)
    assert event["actor_id"] == "recepcion-01"
    assert event["actor_type"] == "staff"
    assert event["correlation_id"] == "corr-resched-1"


def test_reschedule_audit_before_state_holds_the_old_interval(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    reschedule_appointment(session, appointment_id, local(10))

    before = reschedule_actions(session)[0]["before_state"]
    assert before["start_utc"] == utc_of(9).isoformat()
    assert before["end_utc"] == utc_of(9, 30).isoformat()
    assert before["state"] == "confirmed"


def test_reschedule_audit_after_state_holds_the_new_interval(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    reschedule_appointment(session, appointment_id, local(10))

    after = reschedule_actions(session)[0]["after_state"]
    assert after["start_utc"] == utc_of(10).isoformat()
    assert after["end_utc"] == utc_of(10, 30).isoformat()
    assert after["state"] == "confirmed"


def test_failed_reschedule_leaves_the_appointment_and_the_audit_untouched(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    with pytest.raises(AppError):
        reschedule_appointment(session, appointment_id, local(15))

    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].start_utc == utc_of(9)
    assert rows[0].end_utc == utc_of(9, 30)
    assert rows[0].state == "confirmed"
    assert reschedule_actions(session) == []
    assert count_of(session, AuditEvent) == 1  # only the creation event


def test_reschedule_mutation_and_audit_commit_together_or_not_at_all(
    session, monkeypatch
):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    def boom(*args, **kwargs):
        raise RuntimeError("audit writer exploded")

    monkeypatch.setattr("app.scheduling.service.record_event", boom)

    with pytest.raises(RuntimeError):
        reschedule_appointment(session, appointment_id, local(10))

    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].start_utc == utc_of(9)  # the mutation rolled back with the audit
    assert count_of(session, AuditEvent) == 1


def test_reschedule_never_produces_a_cancelled_twin(session):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    reschedule_appointment(session, appointment_id, local(10))
    reschedule_appointment(session, appointment_id, local(11))

    rows = persisted_appointments(session)
    assert len(rows) == 1
    assert rows[0].id == appointment_id
    assert rows[0].state == "confirmed"
    assert rows[0].start_utc == utc_of(11)
    actions = [event["action"] for event in audit_events(session, appointment_id)]
    assert actions == [
        "appointment.created",
        "appointment.rescheduled",
        "appointment.rescheduled",
    ]


# --- 23-25. real concurrency -------------------------------------------------


BLOCKED_BACKENDS = text(
    "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid <> pg_backend_pid()"
)


def _await_blocked_backends(session, expected, timeout=30.0):
    """Poll (never sleep) until ``expected`` other backends are lock-blocked."""
    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        blocked = session.execute(BLOCKED_BACKENDS).scalar()
        session.rollback()
        if blocked >= expected:
            return True
    return False


def _insert_confirmed(connection_session, ids, start_utc, end_utc):
    connection_session.execute(
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
            "start": start_utc,
            "end": end_utc,
        },
    )


def test_reschedule_defeated_by_a_committed_row_propagates_sqlstate_23P01(
    migrated_engine, session
):
    """The GiST — not the preflight — rejects the late rescheduler.

    A ``SHARE`` table lock lets the worker take its ``FOR UPDATE`` row lock and
    complete the whole preflight (both are compatible with ``SHARE``) while its
    ``UPDATE`` waits. The conflicting confirmed row is then committed
    underneath it, so the ``IntegrityError`` must escape untranslated.
    """
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    outcome = []

    gate = maker()
    gate.execute(text("LOCK TABLE appointments IN SHARE MODE"))

    def attempt():
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            moved = reschedule_appointment(db, appointment_id, local(10))
            outcome.append(("committed", moved.start_utc))
        except Exception as exc:  # noqa: BLE001 - the raised error is the assertion
            db.rollback()
            outcome.append(("failed", exc))
        finally:
            db.close()

    thread = threading.Thread(target=attempt)
    try:
        thread.start()
        blocked_after_preflight = _await_blocked_backends(session, 1)
        _insert_confirmed(gate, ids, utc_of(10), utc_of(10, 30))
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
    assert len(rows) == 2
    original = [row for row in rows if row.id == appointment_id][0]
    assert original.start_utc == utc_of(9)  # untouched
    assert original.state == "confirmed"
    assert reschedule_actions(session) == []  # no orphan audit row


def test_two_concurrent_reschedules_of_the_same_appointment_are_serialized(
    migrated_engine, session
):
    """The row lock — not the constraint — orders same-appointment mutation."""
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    targets = [local(10), local(11)]
    barrier = threading.Barrier(len(targets))
    outcomes = []
    guard = threading.Lock()

    # Hold the appointment row so both workers reach the FOR UPDATE together;
    # from there the lock alone decides the order.
    gate = maker()
    gate.execute(
        text("SELECT id FROM appointments WHERE id = :id FOR UPDATE"),
        {"id": appointment_id},
    )

    def attempt(target):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            moved = reschedule_appointment(db, appointment_id, target)
            with guard:
                outcomes.append(("committed", target, moved.start_utc))
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            db.rollback()
            with guard:
                outcomes.append(("failed", target, exc))
        finally:
            db.close()

    threads = [threading.Thread(target=attempt, args=(target,)) for target in targets]
    try:
        for thread in threads:
            thread.start()
        both_blocked = _await_blocked_backends(session, 2)
    finally:
        gate.commit()
        gate.close()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert both_blocked, outcomes
    assert len(outcomes) == 2, outcomes
    assert all(result[0] == "committed" for result in outcomes), outcomes

    rows = persisted_appointments(session)
    assert len(rows) == 1  # one row, never a torn pair
    final = rows[0]
    assert final.id == appointment_id
    assert final.state == "confirmed"
    assert final.start_utc in {utc_of(10), utc_of(11)}
    assert final.end_utc == final.start_utc + timedelta(minutes=30)

    events = audit_events(session, appointment_id)
    assert [event["action"] for event in events] == [
        "appointment.created",
        "appointment.rescheduled",
        "appointment.rescheduled",
    ]
    first, second = events[1], events[2]
    assert first["before_state"]["start_utc"] == utc_of(9).isoformat()
    # Coherent history: each record starts where the previous one left off, and
    # the last one describes the row that actually survived.
    assert second["before_state"]["start_utc"] == first["after_state"]["start_utc"]
    assert second["before_state"]["end_utc"] == first["after_state"]["end_utc"]
    assert second["after_state"]["start_utc"] == final.start_utc.astimezone(UTC).isoformat()
    assert second["after_state"]["end_utc"] == final.end_utc.astimezone(UTC).isoformat()


def test_cancel_and_reschedule_racing_the_same_appointment_settle_coherently(
    migrated_engine, session
):
    """One final state, one coherent history, whichever operation wins the lock."""
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = {}
    guard = threading.Lock()

    gate = maker()
    gate.execute(
        text("SELECT id FROM appointments WHERE id = :id FOR UPDATE"),
        {"id": appointment_id},
    )

    def run(name, operation):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        try:
            barrier.wait(timeout=20)
            result = operation(db)
            with guard:
                outcomes[name] = ("committed", result.state, result.start_utc)
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            db.rollback()
            with guard:
                outcomes[name] = ("failed", exc, None)
        finally:
            db.close()

    threads = [
        threading.Thread(
            target=run, args=("cancel", lambda db: cancel_appointment(db, appointment_id))
        ),
        threading.Thread(
            target=run,
            args=(
                "reschedule",
                lambda db: reschedule_appointment(db, appointment_id, local(10)),
            ),
        ),
    ]
    try:
        for thread in threads:
            thread.start()
        both_blocked = _await_blocked_backends(session, 2)
    finally:
        gate.commit()
        gate.close()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert both_blocked, outcomes
    assert set(outcomes) == {"cancel", "reschedule"}, outcomes

    rows = persisted_appointments(session)
    assert len(rows) == 1
    final = rows[0]
    assert final.id == appointment_id

    events = audit_events(session, appointment_id)
    actions = [event["action"] for event in events]
    assert actions[0] == "appointment.created"
    assert actions.count("appointment.cancelled") == 1

    if outcomes["reschedule"][0] == "committed":
        # The rescheduler won the lock; the canceller then cancelled the moved row.
        assert outcomes["cancel"][0] == "committed", outcomes
        assert actions == [
            "appointment.created",
            "appointment.rescheduled",
            "appointment.cancelled",
        ]
        assert final.state == "cancelled"
        assert final.start_utc == utc_of(10)
    else:
        # The canceller won: the rescheduler saw the state it read under the row
        # lock and refused deterministically.
        assert outcomes["cancel"][0] == "committed", outcomes
        loser = outcomes["reschedule"][1]
        assert isinstance(loser, AppError), repr(loser)
        assert loser.code is ErrorCode.ENTITY_INACTIVE
        assert actions == ["appointment.created", "appointment.cancelled"]
        assert final.state == "cancelled"
        assert final.start_utc == utc_of(9)  # the interval is preserved

    last = events[-1]
    assert last["after_state"]["start_utc"] == final.start_utc.astimezone(UTC).isoformat()
    assert last["after_state"]["state"] == final.state


# --- 26-32. API --------------------------------------------------------------


@pytest.fixture
def api_app(migrated_engine):
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    return app


@pytest.fixture
def client(api_app):
    return TestClient(api_app, raise_server_exceptions=False)


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_reschedule_endpoint_returns_200_with_the_typed_appointment(session, client):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id

    response = client.post(
        f"/appointments/{appointment_id}/reschedule",
        json={"new_start": utc_of(10).isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == appointment_id
    assert body["state"] == "confirmed"
    assert _dt(body["start_utc"]) == utc_of(10)
    assert _dt(body["end_utc"]) == utc_of(10, 30)
    assert body["lead_id"] == ids["lead_id"]
    rows = persisted_appointments(session)
    assert len(rows) == 1


@pytest.mark.parametrize(
    "extra",
    [{"duration_minutes": 15}, {"end": "2026-08-10T15:15:00Z"}, {"state": "cancelled"}],
)
def test_reschedule_schema_forbids_duration_end_and_state(session, client, extra):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    payload = {"new_start": utc_of(10).isoformat()}
    payload.update(extra)

    response = client.post(f"/appointments/{appointment_id}/reschedule", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    rows = persisted_appointments(session)
    assert rows[0].start_utc == utc_of(9)  # nothing moved


def test_reschedule_endpoint_missing_appointment_returns_404_envelope(session, client):
    seed(session)

    response = client.post(
        "/appointments/999999/reschedule", json={"new_start": utc_of(10).isoformat()}
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Appointment not found.",
            "details": {},
        }
    }


def test_reschedule_endpoint_blocked_slot_returns_409_envelope(session, client):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    book(session, ids, start=local(10))

    response = client.post(
        f"/appointments/{appointment_id}/reschedule",
        json={"new_start": utc_of(10).isoformat()},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "SLOT_BLOCKED"
    assert body["error"]["details"] == {}
    assert "Traceback" not in response.text


def test_reschedule_endpoint_cancelled_appointment_returns_409_envelope(
    session, client
):
    ids = seed(session)
    appointment = book(session, ids, start=local(9))
    appointment_id = appointment.id
    cancel_appointment(session, appointment_id)

    response = client.post(
        f"/appointments/{appointment_id}/reschedule",
        json={"new_start": utc_of(10).isoformat()},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ENTITY_INACTIVE"


def test_openapi_exposes_both_task_9_routes(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    for path in (
        "/appointments",
        "/appointments/{appointment_id}/cancel",
        "/appointments/{appointment_id}/reschedule",
    ):
        assert path in spec["paths"], path
    assert "post" in spec["paths"]["/appointments/{appointment_id}/cancel"]
    assert "post" in spec["paths"]["/appointments/{appointment_id}/reschedule"]

    schemas = spec["components"]["schemas"]
    for name in ("AppointmentRead", "AppointmentReschedule", "AppointmentCancel"):
        assert name in schemas, name
    assert schemas["AppointmentReschedule"]["required"] == ["new_start"]
    assert set(schemas["AppointmentReschedule"]["properties"]) == {"new_start"}
    assert schemas["AppointmentReschedule"].get("additionalProperties") is False


def test_health_unchanged(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
