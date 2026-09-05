"""PF4 — durable command idempotency proofs against real PostgreSQL.

The block makes ``appointments.book``, ``appointments.reschedule`` and
``appointments.cancel`` durably exactly-once per ``(organization_id,
operation, idempotency_key)`` through the PostgreSQL ``command_receipts``
table (PF0 spec §15–§16). The unique index is the whole concurrency
mechanism: no PENDING state machine, no polling, no advisory locks, no
sleeps — concurrent proofs use threads + ``threading.Barrier`` like the rest
of the suite (AGENTS.md §6).

Proven semantics:

* C1  — same key + same fingerprint, concurrent → executed exactly once
  (one appointment, one audit row, one receipt; both callers see the same
  logical outcome);
* C2  — same key + different fingerprint → deterministic
  ``IDEMPOTENCY_KEY_REUSED``, zero new rows;
* C3  — a rolled-back command leaves no receipt; a later retry executes;
* C4  — sequential retry after success replays the stored outcome (booking
  no longer answers 409 to its own retry; cancel/reschedule append no
  duplicate audit rows);
* C5  — different keys on the same slot still settle via the GiST exclusion
  exactly as today;
* C6  — cross-principal replay is refused;
* C7  — a non-receipt ``23505`` is not treated as an idempotency event;
* C8  — the booking ``40P01`` one-shot retry re-claims cleanly after
  rollback;
* C9  — the replay read happens in a separate transaction; sessions stay
  idle afterwards;
* I2  — keys are tenant-scoped;
* I4  — fingerprints are canonical and exclude transport noise;
* I10 — agent/integration principals must supply a key;
* I11 — an absent key keeps today's behaviour byte-for-byte.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.idempotency.models import CommandReceipt
from app.idempotency.service import (
    OP_APPOINTMENTS_BOOK,
    OP_APPOINTMENTS_CANCEL,
    OP_APPOINTMENTS_RESCHEDULE,
    command_fingerprint,
    run_idempotent_command,
)
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    APPOINTMENTS_CANCEL,
    APPOINTMENTS_CREATE,
    APPOINTMENTS_RESCHEDULE,
)
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
)
from app.organization.models import (
    Location,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.service import create_organization
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
RULE_WINDOW = (time(9, 0), time(13, 0))

KEY_BOOK = "key-booking-0001"
KEY_CANCEL = "key-cancel-0001"
KEY_RESCHEDULE = "key-reschedule-0001"


def local(hour, minute=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def utc_of(hour, minute=0, day=MONDAY):
    return local(hour, minute, day).astimezone(UTC)


def seed_booking(session, *, organization_id=ORG):
    """One complete, bookable tenant chain through the application services."""
    service = Service(
        organization_id=organization_id,
        name=f"Servicio {organization_id}",
        duration_minutes=30,
        is_active=True,
    )
    location = Location(
        organization_id=organization_id,
        name=f"Sede {organization_id}",
        timezone=LIMA,
        is_active=True,
    )
    practitioner = Practitioner(display_name="Dra. Ana", is_active=True)
    lead = Lead(
        organization_id=organization_id,
        full_name="Juan Pérez",
        contact_phone="+51999000111",
        acquisition_source="direct",
    )
    session.add_all([service, location, practitioner, lead])
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=organization_id, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.flush()
    session.add(
        PractitionerCapability(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            service_id=service.id,
            location_id=location.id,
            is_active=True,
        )
    )
    session.add(
        AvailabilityRule(
            organization_id=organization_id,
            practitioner_id=practitioner.id,
            location_id=location.id,
            day_of_week=0,
            start_local=RULE_WINDOW[0],
            end_local=RULE_WINDOW[1],
        )
    )
    session.commit()
    return {
        "organization_id": organization_id,
        "lead_id": lead.id,
        "service_id": service.id,
        "location_id": location.id,
        "practitioner_id": practitioner.id,
    }


def seed_actor(session, *, organization_id=ORG, principal_type="human", codes=()):
    """A tenant principal holding the given permission codes, org-wide.

    Returns plain values (principal id, principal type) so the session is left
    **idle**: the IAM provisioning helpers end with a refresh, which opens a
    transaction, and the mutation under test demands an idle Session (A2).
    """
    principal = create_principal(
        session,
        display_name=f"{principal_type} actor",
        principal_type=principal_type,
    )
    membership = add_membership(session, organization_id=organization_id, principal_id=principal.id)
    role_code = f"role-{principal_type}-{principal.id}"
    role = create_role(
        session, organization_id=organization_id, code=role_code, name=principal_type
    )
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(
        session,
        organization_id=organization_id,
        membership_id=membership.id,
        role_id=role.id,
    )
    values = (principal.id, principal.type)
    session.rollback()
    return values


def outcome_resource_id(outcome) -> int:
    """The resource id of a command outcome, executed or replayed."""
    if outcome.result is not None:
        return outcome.result.id
    return int(outcome.outcome["resource_id"])


def ctx_for(principal_id, principal_type, organization_id, *, correlation_id="corr-test"):
    return ExecutionContext(
        organization_id=organization_id,
        principal_id=principal_id,
        principal_type=principal_type,
        request_id="req-test",
        correlation_id=correlation_id,
    )


def system_ctx(organization_id=ORG):
    return ctx_for(1, "system", organization_id)


def count_of(session, model):
    total = session.scalar(select(func.count()).select_from(model))
    session.rollback()
    return total


def receipt_rows(session, *, operation=None):
    query = select(CommandReceipt).order_by(CommandReceipt.id)
    if operation is not None:
        query = query.where(CommandReceipt.operation == operation)
    rows = list(session.scalars(query))
    session.rollback()
    return rows


def audit_rows(session, action=None):
    query = select(AuditEvent).order_by(AuditEvent.id)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    rows = list(session.scalars(query))
    session.rollback()
    return rows


def book_payload(ids, start_utc):
    return {
        "lead_id": ids["lead_id"],
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "practitioner_id": ids["practitioner_id"],
        "start": start_utc,
    }


# --- C1: concurrent same-key booking executes exactly once -------------------


def test_concurrent_same_key_booking_executes_exactly_once(migrated_engine, session):
    ids = seed_booking(session)
    (principal_id, _) = seed_actor(session, codes=(APPOINTMENTS_CREATE,))
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt():
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        ctx = ctx_for(principal_id, "human", ORG)
        payload = book_payload(ids, utc_of(9))
        try:
            barrier.wait(timeout=20)
            outcome = run_idempotent_command(
                db,
                operation=book_appointment,
                operation_name=OP_APPOINTMENTS_BOOK,
                key=KEY_BOOK,
                ctx=ctx,
                params=payload,
                **payload,
            )
            with guard:
                outcomes.append((outcome.replayed, outcome_resource_id(outcome)))
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            db.rollback()
            with guard:
                outcomes.append(("failed", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert len(outcomes) == 2, outcomes
    assert all(not isinstance(o[1], Exception) for o in outcomes), outcomes

    executed = [o for o in outcomes if o[0] is False]
    replayed = [o for o in outcomes if o[0] is True]
    assert len(executed) == 1, outcomes
    assert len(replayed) == 1, outcomes
    assert executed[0][1] == replayed[0][1], outcomes

    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    receipts = receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)
    assert len(receipts) == 1
    assert receipts[0].outcome_json["resource_id"] == str(executed[0][1])


# --- C1: concurrent same-key reschedule executes exactly once ----------------


def test_concurrent_same_key_reschedule_executes_exactly_once(migrated_engine, session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))
    booked = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    appointment_id = booked.result.id
    new_start = utc_of(10)

    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt():
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        params = {"appointment_id": appointment_id, "new_start": new_start}
        try:
            barrier.wait(timeout=20)
            outcome = run_idempotent_command(
                db,
                operation=reschedule_appointment,
                operation_name=OP_APPOINTMENTS_RESCHEDULE,
                key=KEY_RESCHEDULE,
                ctx=ctx,
                params=params,
                **params,
            )
            with guard:
                outcomes.append((outcome.replayed, outcome_resource_id(outcome)))
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            db.rollback()
            with guard:
                outcomes.append(("failed", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert len(outcomes) == 2, outcomes
    assert all(not isinstance(o[1], Exception) for o in outcomes), outcomes

    executed = [o for o in outcomes if o[0] is False]
    replayed = [o for o in outcomes if o[0] is True]
    assert len(executed) == 1, outcomes
    assert len(replayed) == 1, outcomes
    assert executed[0][1] == replayed[0][1] == appointment_id, outcomes

    moved = session.scalar(select(Appointment).where(Appointment.id == appointment_id))
    session.rollback()
    assert moved.start_utc == new_start
    rows = audit_rows(session, action="appointment.rescheduled")
    assert len(rows) == 1
    assert rows[0].before_state["start_utc"] != rows[0].after_state["start_utc"]
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_RESCHEDULE)) == 1


# --- C4: sequential replay returns the stored outcome ------------------------


def test_sequential_booking_replay_returns_stored_outcome(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))

    first = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    assert first.replayed is False
    assert first.result.id is not None

    replay = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    assert replay.replayed is True
    assert replay.outcome["resource_id"] == str(first.result.id)
    assert replay.outcome["state"] == "confirmed"

    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


def test_booking_replay_via_http_returns_original_outcome_and_replay_header(
    migrated_engine, session
):
    ids = seed_booking(session)
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db

    app.state.auth_sessionmaker = maker
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)
    headers = {"Idempotency-Key": KEY_BOOK}
    payload = book_payload(ids, utc_of(9))
    payload["start"] = payload["start"].isoformat()

    first = client.post("/appointments", json=payload, headers=headers)
    second = client.post("/appointments", json=payload, headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"
    assert first.headers.get("Idempotent-Replay") is None
    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


def test_cancel_replay_returns_stored_outcome_without_duplicate_audit(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))
    booked = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )

    cancel_params = {"appointment_id": booked.result.id}
    cancelled = run_idempotent_command(
        session,
        operation=cancel_appointment,
        operation_name=OP_APPOINTMENTS_CANCEL,
        key=KEY_CANCEL,
        ctx=ctx,
        params=cancel_params,
        **cancel_params,
    )
    assert cancelled.replayed is False
    assert cancelled.result.state == "cancelled"

    replay = run_idempotent_command(
        session,
        operation=cancel_appointment,
        operation_name=OP_APPOINTMENTS_CANCEL,
        key=KEY_CANCEL,
        ctx=ctx,
        params=cancel_params,
        **cancel_params,
    )
    assert replay.replayed is True
    assert replay.outcome["resource_id"] == str(booked.result.id)
    assert replay.outcome["state"] == "cancelled"

    assert len(audit_rows(session, action="appointment.cancelled")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_CANCEL)) == 1
    assert count_of(session, Appointment) == 1


def test_reschedule_replay_returns_stored_outcome_without_duplicate_audit(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))
    booked = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )

    new_start = utc_of(10)
    reschedule_params = {"appointment_id": booked.result.id, "new_start": new_start}
    rescheduled = run_idempotent_command(
        session,
        operation=reschedule_appointment,
        operation_name=OP_APPOINTMENTS_RESCHEDULE,
        key=KEY_RESCHEDULE,
        ctx=ctx,
        params=reschedule_params,
        **reschedule_params,
    )
    assert rescheduled.replayed is False
    assert rescheduled.result.start_utc == new_start

    replay = run_idempotent_command(
        session,
        operation=reschedule_appointment,
        operation_name=OP_APPOINTMENTS_RESCHEDULE,
        key=KEY_RESCHEDULE,
        ctx=ctx,
        params=reschedule_params,
        **reschedule_params,
    )
    assert replay.replayed is True
    assert replay.outcome["resource_id"] == str(booked.result.id)
    assert replay.outcome["start_utc"] == new_start.isoformat()

    rows = audit_rows(session, action="appointment.rescheduled")
    assert len(rows) == 1
    assert rows[0].before_state["start_utc"] != rows[0].after_state["start_utc"]
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_RESCHEDULE)) == 1


# --- C2: same key, different fingerprint → deterministic rejection -----------


def test_same_key_different_fingerprint_rejected_without_mutation(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))

    first = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    assert first.replayed is False

    changed = book_payload(ids, utc_of(10))
    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=KEY_BOOK,
            ctx=ctx,
            params=changed,
            **changed,
        )
    assert exc.value.code == ErrorCode.IDEMPOTENCY_KEY_REUSED
    assert exc.value.http_status == 409
    assert exc.value.details == {}

    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


def test_same_key_different_fingerprint_via_http_is_409_idempotency_key_reused(
    migrated_engine, session
):
    ids = seed_booking(session)
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db

    app.state.auth_sessionmaker = maker
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)
    headers = {"Idempotency-Key": KEY_BOOK}

    def book(start):
        payload = book_payload(ids, start)
        payload["start"] = payload["start"].isoformat()
        return client.post("/appointments", json=payload, headers=headers)

    assert book(utc_of(9)).status_code == 201
    second = book(utc_of(10))
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert second.json()["error"]["details"] == {}
    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1


# --- C3: rollback leaves no receipt; retry executes --------------------------


def test_rolled_back_command_leaves_no_receipt_then_retry_executes(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))

    # Block the slot so the command fails its preflight AFTER claiming.
    session.add(
        ScheduleBlock(
            organization_id=ORG,
            practitioner_id=ids["practitioner_id"],
            location_id=ids["location_id"],
            start_utc=utc_of(8, 30),
            end_utc=utc_of(10),
        )
    )
    session.commit()

    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=KEY_BOOK,
            ctx=ctx,
            params=payload,
            **payload,
        )
    assert exc.value.code == ErrorCode.SLOT_BLOCKED
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 0
    assert count_of(session, Appointment) == 0
    assert len(audit_rows(session, action="appointment.created")) == 0

    # The block is removed; the same key now executes instead of replaying a
    # failure that was never stored (I7: only success is memoized).
    session.execute(text("DELETE FROM schedule_blocks"))
    session.commit()

    retry = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    assert retry.replayed is False
    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


# --- C5: different keys on the same slot still settle via the GiST -----------


def test_different_keys_same_slot_sequential_is_slot_blocked(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))
    run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )

    other = book_payload(ids, utc_of(9))
    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key="key-booking-0002",
            ctx=ctx,
            params=other,
            **other,
        )
    assert exc.value.code == ErrorCode.SLOT_BLOCKED
    assert count_of(session, Appointment) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


def test_concurrent_different_keys_same_slot_settle_by_gist(migrated_engine, session):
    ids = seed_booking(session)
    (principal_id, _) = seed_actor(session, codes=(APPOINTMENTS_CREATE,))
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []
    guard = threading.Lock()

    def attempt(key):
        db = maker()
        db.execute(text("SELECT 1"))
        db.commit()
        ctx = ctx_for(principal_id, "human", ORG)
        payload = book_payload(ids, utc_of(9))
        try:
            barrier.wait(timeout=20)
            outcome = run_idempotent_command(
                db,
                operation=book_appointment,
                operation_name=OP_APPOINTMENTS_BOOK,
                key=key,
                ctx=ctx,
                params=payload,
                **payload,
            )
            with guard:
                outcomes.append(("committed", outcome.result.id))
        except Exception as exc:  # noqa: BLE001 - the loser's error is the assertion
            db.rollback()
            with guard:
                outcomes.append(("failed", exc))
        finally:
            db.close()

    threads = [
        threading.Thread(target=attempt, args=("key-booking-a-0001",)),
        threading.Thread(target=attempt, args=("key-booking-b-0001",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    committed = [o for o in outcomes if o[0] == "committed"]
    failed = [o for o in outcomes if o[0] == "failed"]
    assert len(committed) == 1, outcomes
    assert len(failed) == 1, outcomes
    loser = failed[0][1]
    assert isinstance(loser, (IntegrityError, OperationalError)), repr(loser)
    assert loser.orig.sqlstate in {"23P01", "40P01"}, repr(loser)

    assert count_of(session, Appointment) == 1
    # Only the winner's claim survives; the loser's receipt rolled back with
    # its transaction (C3 applied to the exclusion-conflict path).
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


# --- C6: cross-principal replay is refused -----------------------------------


def test_cross_principal_replay_is_refused(session):
    ids = seed_booking(session)
    (human_a, _) = seed_actor(session, principal_type="human", codes=(APPOINTMENTS_CREATE,))
    (human_b, _) = seed_actor(session, principal_type="human", codes=(APPOINTMENTS_CREATE,))
    payload = book_payload(ids, utc_of(9))

    first = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx_for(human_a, "human", ORG),
        params=payload,
        **payload,
    )
    assert first.replayed is False

    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=KEY_BOOK,
            ctx=ctx_for(human_b, "human", ORG),
            params=payload,
            **payload,
        )
    assert exc.value.code == ErrorCode.IDEMPOTENCY_KEY_REUSED
    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


# --- authorization cannot be bypassed ----------------------------------------


def test_keyed_command_still_enforces_authorization(session):
    ids = seed_booking(session)
    # A member with a membership but no permission at all (system principal is
    # skipped: it holds everything). The claim insert succeeds (membership is
    # structural) but the authoritative permission check denies before any
    # mutation — and the rollback removes the claim with it.
    (no_permission, _) = seed_actor(session, principal_type="human", codes=())
    payload = book_payload(ids, utc_of(9))

    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=KEY_BOOK,
            ctx=ctx_for(no_permission, "human", ORG),
            params=payload,
            **payload,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    assert count_of(session, Appointment) == 0
    assert len(audit_rows(session, action="appointment.created")) == 0
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 0


# --- I2: keys are tenant-scoped ----------------------------------------------


def test_same_key_in_two_organizations_executes_independently(session):
    org_b = create_organization(session, name="Segunda Clínica").id
    ids_a = seed_booking(session, organization_id=ORG)
    ids_b = seed_booking(session, organization_id=org_b)
    (human_a, _) = seed_actor(session, organization_id=ORG, codes=(APPOINTMENTS_CREATE,))
    (human_b, _) = seed_actor(session, organization_id=org_b, codes=(APPOINTMENTS_CREATE,))
    payload_a = book_payload(ids_a, utc_of(9))
    payload_b = book_payload(ids_b, utc_of(9))

    first = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key="key-shared-0001",
        ctx=ctx_for(human_a, "human", ORG),
        params=payload_a,
        **payload_a,
    )
    second = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key="key-shared-0001",
        ctx=ctx_for(human_b, "human", org_b),
        params=payload_b,
        **payload_b,
    )
    assert first.replayed is False
    assert second.replayed is False
    assert count_of(session, Appointment) == 2
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 2


# --- I11 / I10: key requirement policy ---------------------------------------


def test_absent_key_keeps_today_behaviour_and_writes_no_receipt(session):
    ids = seed_booking(session)
    payload = book_payload(ids, utc_of(9))
    appointment = book_appointment(session, ctx=system_ctx(), **payload)
    assert appointment.id is not None
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 0


def test_agent_principal_without_key_rejected_before_mutation(session):
    ids = seed_booking(session)
    (agent, _) = seed_actor(session, principal_type="agent", codes=(APPOINTMENTS_CREATE,))
    payload = book_payload(ids, utc_of(9))

    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=None,
            ctx=ctx_for(agent, "agent", ORG),
            params=payload,
            **payload,
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert count_of(session, Appointment) == 0
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 0


@pytest.mark.parametrize(
    "key",
    (
        "derived-from-business-data",
        str(uuid.uuid1()),
        str(uuid.uuid4()).upper(),
        "00000000-0000-4000-8000-000000000000-extra",
    ),
)
def test_agent_principal_requires_a_uuid4_idempotency_key(session, key):
    ids = seed_booking(session)
    (agent, _) = seed_actor(session, principal_type="agent", codes=(APPOINTMENTS_CREATE,))
    payload = book_payload(ids, utc_of(9))

    with pytest.raises(AppError) as exc:
        run_idempotent_command(
            session,
            operation=book_appointment,
            operation_name=OP_APPOINTMENTS_BOOK,
            key=key,
            ctx=ctx_for(agent, "agent", ORG),
            params=payload,
            **payload,
        )

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert count_of(session, Appointment) == 0
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 0


def test_agent_principal_accepts_a_uuid4_idempotency_key(session):
    ids = seed_booking(session)
    (agent, _) = seed_actor(session, principal_type="agent", codes=(APPOINTMENTS_CREATE,))
    payload = book_payload(ids, utc_of(9))

    result = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=str(uuid.uuid4()),
        ctx=ctx_for(agent, "agent", ORG),
        params=payload,
        **payload,
    )

    assert result.replayed is False
    assert count_of(session, Appointment) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


# --- C8: the booking 40P01 retry re-claims cleanly ---------------------------


class _Fake40P01:
    sqlstate = "40P01"


def test_40p01_retry_reclaims_cleanly(migrated_engine, session):
    """The transport's one-shot deadlock retry still works with idempotency.

    The first attempt aborts with ``40P01`` (simulated exactly like
    ``test_api.py``); its claim — inside the aborted transaction — vanishes
    (C3). The retry re-claims the same key and executes; if the claim had
    survived, the retry would replay (or reject) instead of booking, so one
    appointment plus one receipt is the proof that the re-claim was clean.
    """
    ids = seed_booking(session)
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db

    app.state.auth_sessionmaker = maker

    calls = []
    real_operation = book_appointment

    def fake_op(session, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, _Fake40P01())
        return real_operation(session, **kwargs)

    from app.scheduling.router import get_booking_operation

    app.dependency_overrides[get_booking_operation] = lambda: fake_op
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)
    payload = book_payload(ids, utc_of(9))
    payload["start"] = payload["start"].isoformat()

    response = client.post(
        "/appointments", json=payload, headers={"Idempotency-Key": KEY_BOOK}
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "confirmed"
    assert len(calls) == 2
    assert count_of(session, Appointment) == 1
    assert len(audit_rows(session, action="appointment.created")) == 1
    assert len(receipt_rows(session, operation=OP_APPOINTMENTS_BOOK)) == 1


# --- C7: a non-receipt 23505 is not an idempotency event ---------------------


def test_non_receipt_23505_is_not_an_idempotency_event(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    existing_name = session.scalar(
        select(Service.name).where(Service.organization_id == ids["organization_id"]).limit(1)
    )
    session.rollback()

    def duplicate_service_operation(session, **kwargs):
        session.execute(
            text(
                "INSERT INTO services (organization_id, name, duration_minutes) "
                "VALUES (:org, :name, 30)"
            ),
            {"org": ids["organization_id"], "name": existing_name},
        )
        session.commit()

    payload = {"anything": 1}
    with pytest.raises(IntegrityError) as exc:
        run_idempotent_command(
            session,
            operation=duplicate_service_operation,
            operation_name="services.create",
            key="key-services-0001",
            ctx=ctx,
            params=payload,
            **payload,
        )
    assert exc.value.orig.sqlstate == "23505"
    assert exc.value.orig.diag.constraint_name == "uq_services_organization_name"
    session.rollback()
    assert len(receipt_rows(session, operation="services.create")) == 0


# --- C9: sessions stay idle; replay reads in a separate transaction ----------


def test_replay_and_execute_leave_session_idle(session):
    ids = seed_booking(session)
    ctx = system_ctx()
    payload = book_payload(ids, utc_of(9))

    first = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    assert first.replayed is False
    assert session.in_transaction() is False

    replay = run_idempotent_command(
        session,
        operation=book_appointment,
        operation_name=OP_APPOINTMENTS_BOOK,
        key=KEY_BOOK,
        ctx=ctx,
        params=payload,
        **payload,
    )
    assert replay.replayed is True
    assert session.in_transaction() is False


# --- I4: fingerprint canonicalization ----------------------------------------


def test_fingerprint_is_canonical_and_excludes_transport_noise():
    base = {
        "operation": OP_APPOINTMENTS_BOOK,
        "organization_id": ORG,
        "params": {
            "lead_id": 1,
            "service_id": 2,
            "location_id": 3,
            "practitioner_id": 4,
            "start": utc_of(9),
        },
    }
    same_instant_other_tz = {
        "lead_id": 1,
        "service_id": 2,
        "location_id": 3,
        "practitioner_id": 4,
        "start": local(9).astimezone(UTC),
    }
    assert (
        command_fingerprint(
            operation=base["operation"], organization_id=ORG, params=same_instant_other_tz
        )
        == command_fingerprint(
            operation=base["operation"], organization_id=ORG, params=base["params"]
        )
    )
    later = command_fingerprint(
        operation=base["operation"],
        organization_id=ORG,
        params={**base["params"], "start": utc_of(10)},
    )
    assert later != command_fingerprint(
        operation=base["operation"], organization_id=ORG, params=base["params"]
    )
    # The fingerprint is stable across runs (byte-for-byte canonical JSON).
    assert (
        command_fingerprint(
            operation=base["operation"], organization_id=ORG, params=base["params"]
        )
        == command_fingerprint(
            operation=base["operation"], organization_id=ORG, params=base["params"]
        )
    )
