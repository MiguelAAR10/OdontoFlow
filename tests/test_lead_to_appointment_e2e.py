"""Task 10 — Lead-to-Appointment vertical E2E closure (proof).

Every business action is performed exclusively through the public HTTP API
(``TestClient(create_app())``) against real PostgreSQL via the conftest
``migrated_engine`` / ``clean_tables`` fixtures. Direct SQLAlchemy reads are
used only for final verification and audit evidence — never for writes.

The scenario is deterministic: a fixed future date (2026-09-01, a Tuesday) is
derived by weekday arithmetic, never from the wall clock, and every interval
is expressed in timezone-aware UTC. The eligibility split is deliberately
non-generic: Dra. Ana can only perform 'Consulta Ortodoncia' at 'Sede A' and
Dr. Luis only 'Evaluacion Inicial' at 'Sede B', proving that eligibility and
availability are real domain decisions, not a lookup that matches everyone.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.audit.models import AuditEvent
from app.catalog.models import Service
from app.commercial.models import Lead
from app.db import get_db
from app.organization.models import Location, Practitioner, PractitionerCapability
from app.scheduling.models import Appointment

LIMA = "America/Lima"
UTC = timezone.utc

# Deterministic scenario clock: first date >= 2026-09-01 whose weekday is 1
# (Tuesday). 2026-09-01 IS a Tuesday, so the chosen date is fixed and stable.
DAY_OF_WEEK = 1
BASE_DATE = date(2026, 9, 1)
RULE_START = time(9, 0)
RULE_END = time(12, 0)
DURATION_MINUTES = 45
GRID_MINUTES = 15


def _chosen_date() -> date:
    delta = (DAY_OF_WEEK - BASE_DATE.weekday()) % 7
    return BASE_DATE + timedelta(days=delta)


def _local(date_, t) -> datetime:
    return datetime(date_.year, date_.month, date_.day, t.hour, t.minute, tzinfo=ZoneInfo(LIMA))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _window() -> tuple[str, str]:
    """The UTC window covering 09:00-12:00 America/Lima on the chosen date."""
    chosen = _chosen_date()
    start = _local(chosen, RULE_START).astimezone(UTC)
    end = _local(chosen, RULE_END).astimezone(UTC)
    return _iso(start), _iso(end)


def _expected_slots() -> list[tuple[datetime, datetime]]:
    """The API-computed candidates derived from the Task 6 rule semantics:
    15-minute grid on the location wall clock, whole interval inside the rule,
    half-open ``[start, end)``, duration taken from the catalog service."""
    chosen = _chosen_date()
    slots: list[tuple[datetime, datetime]] = []
    tick = RULE_START.hour * 60 + RULE_START.minute
    end_min = RULE_END.hour * 60 + RULE_END.minute
    while tick + DURATION_MINUTES <= end_min:
        local = datetime(chosen.year, chosen.month, chosen.day, tick // 60, tick % 60, tzinfo=ZoneInfo(LIMA))
        start = local.astimezone(UTC)
        slots.append((start, start + timedelta(minutes=DURATION_MINUTES)))
        tick += GRID_MINUTES
    return slots


@pytest.fixture
def api_app(migrated_engine):
    """A fresh OdontoFlow app whose ``get_db`` binds to the test database."""
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    return app, maker


@pytest.fixture
def client(api_app):
    app, _maker = api_app
    return TestClient(app, raise_server_exceptions=False)


def _seed(client) -> dict:
    """Create services, locations, practitioners, capabilities and the lead
    entirely through the HTTP API. Returns the created resource map."""
    def _post(path, payload):
        response = client.post(path, json=payload)
        assert response.status_code == 201, (path, response.text)
        return response.json()

    services = {
        "eval": _post("/services", {"name": "Evaluacion Inicial", "duration_minutes": 30}),
        "consulta": _post("/services", {"name": "Consulta Ortodoncia", "duration_minutes": DURATION_MINUTES}),
    }
    locations = {
        "sede_a": _post("/locations", {"name": "Sede A", "timezone": LIMA}),
        "sede_b": _post("/locations", {"name": "Sede B", "timezone": LIMA}),
    }
    practitioners = {
        "ana": _post("/practitioners", {"display_name": "Dra. Ana"}),
        "luis": _post("/practitioners", {"display_name": "Dr. Luis"}),
    }
    capabilities = {
        "ana_consulta_a": _post(
            "/capabilities",
            {
                "practitioner_id": practitioners["ana"]["id"],
                "service_id": services["consulta"]["id"],
                "location_id": locations["sede_a"]["id"],
            },
        ),
        "luis_eval_b": _post(
            "/capabilities",
            {
                "practitioner_id": practitioners["luis"]["id"],
                "service_id": services["eval"]["id"],
                "location_id": locations["sede_b"]["id"],
            },
        ),
    }
    lead = _post(
        "/leads",
        {
            "full_name": "Juan Perez",
            "contact_phone": "+51 999 000 111",
            "acquisition_source": "promotion",
            "service_need_id": services["consulta"]["id"],
        },
    )
    return {
        "services": services,
        "locations": locations,
        "practitioners": practitioners,
        "capabilities": capabilities,
        "lead": lead,
    }


def _query_slots(client, service_id, location_id):
    window_start, window_end = _window()
    response = client.post(
        "/slots/query",
        json={
            "service_id": service_id,
            "location_id": location_id,
            "window_start": window_start,
            "window_end": window_end,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _slot_keys(slot) -> set[str]:
    return set(slot.keys())


def _disjoint(left: tuple[datetime, datetime], right: tuple[datetime, datetime]) -> bool:
    """Half-open ``[start, end)`` intervals do not intersect."""
    return left[1] <= right[0] or left[0] >= right[1]


def test_lead_to_appointment_e2e_full_journey(client, session):
    chosen = _chosen_date()
    assert chosen.weekday() == DAY_OF_WEEK
    assert chosen >= BASE_DATE

    ids = _seed(client)
    services = ids["services"]
    locations = ids["locations"]
    practitioners = ids["practitioners"]
    lead = ids["lead"]

    # --- step 1. services: typed 201, authoritative durations ----------------
    assert services["eval"]["duration_minutes"] == 30
    assert services["consulta"]["duration_minutes"] == 45
    assert services["consulta"]["name"] == "Consulta Ortodoncia"
    listed = client.get("/services")
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()}
    assert {"Evaluacion Inicial", "Consulta Ortodoncia"} <= names

    # --- step 2. locations, practitioners, capabilities (HTTP only) ----------
    assert locations["sede_a"]["timezone"] == LIMA
    assert locations["sede_b"]["timezone"] == LIMA
    assert practitioners["ana"]["display_name"] == "Dra. Ana"
    assert practitioners["luis"]["display_name"] == "Dr. Luis"
    assert ids["capabilities"]["ana_consulta_a"]["is_active"] is True
    assert ids["capabilities"]["luis_eval_b"]["is_active"] is True

    # --- step 3. deterministic, non-generic eligibility ----------------------
    def _eligible(service_id, location_id):
        response = client.get(
            "/practitioners/eligible",
            params={"service_id": service_id, "location_id": location_id},
        )
        assert response.status_code == 200, response.text
        return [p["id"] for p in response.json()]

    assert _eligible(services["consulta"]["id"], locations["sede_a"]["id"]) == [
        practitioners["ana"]["id"]
    ]
    assert _eligible(services["eval"]["id"], locations["sede_b"]["id"]) == [
        practitioners["luis"]["id"]
    ]
    assert _eligible(services["eval"]["id"], locations["sede_a"]["id"]) == []

    # --- step 4. lead created and readable, distinct from any patient --------
    assert lead["full_name"] == "Juan Perez"
    # The Task 5 commercial service canonicalizes the raw phone
    # "+51 999 000 111" into its stored form "+51999000111".
    assert lead["contact_phone"] == "+51999000111"
    assert lead["acquisition_source"] == "promotion"
    assert lead["service_need_id"] == services["consulta"]["id"]
    fetched = client.get(f"/leads/{lead['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == lead["id"]
    assert fetched.json()["commercial_status"] == "new"
    # No patient concept exists anywhere: only the lead created it.
    assert session.scalar(select(func.count()).select_from(Lead)) == 1

    # --- step 5. availability rule: Dra. Ana @ Sede A, 09:00-12:00 local -----
    rule = client.post(
        "/availability-rules",
        json={
            "practitioner_id": practitioners["ana"]["id"],
            "location_id": locations["sede_a"]["id"],
            "day_of_week": DAY_OF_WEEK,
            "start_local": RULE_START.isoformat(),
            "end_local": RULE_END.isoformat(),
        },
    )
    assert rule.status_code == 201, rule.text
    assert rule.json()["day_of_week"] == DAY_OF_WEEK

    # --- step 6. slot query: API-computed candidates, 15-min grid, 45 min ----
    slots = _query_slots(client, services["consulta"]["id"], locations["sede_a"]["id"])
    expected_all = _expected_slots()
    assert len(slots) == len(expected_all)
    for slot in slots:
        assert _slot_keys(slot) == {"practitioner_id", "start", "end"}
        assert slot["practitioner_id"] == practitioners["ana"]["id"]
    parsed = [(_parse(slot["start"]), _parse(slot["end"])) for slot in slots]
    assert parsed == expected_all
    for start, end in parsed:
        assert start.minute % GRID_MINUTES == 0 and start.second == 0
        assert end - start == timedelta(minutes=DURATION_MINUTES)
    assert [start for start, _ in parsed] == sorted(start for start, _ in parsed)
    slot_a = slots[0]
    interval_a = (_parse(slot_a["start"]), _parse(slot_a["end"]))

    # --- step 7. book the first API-offered slot -----------------------------
    booking = client.post(
        "/appointments",
        json={
            "lead_id": lead["id"],
            "service_id": services["consulta"]["id"],
            "location_id": locations["sede_a"]["id"],
            "practitioner_id": practitioners["ana"]["id"],
            "start": slot_a["start"],
        },
    )
    assert booking.status_code == 201, booking.text
    body = booking.json()
    assert _slot_keys(body) == {
        "id", "lead_id", "service_id", "practitioner_id", "location_id",
        "start_utc", "end_utc", "state",
    }
    appointment_id = body["id"]
    assert body["state"] == "confirmed"
    assert body["start_utc"] == slot_a["start"]
    assert _parse(body["end_utc"]) - _parse(body["start_utc"]) == timedelta(minutes=DURATION_MINUTES)
    assert "duration_minutes" not in body

    # client cannot override duration/end/state on booking (extra='forbid')
    override = client.post(
        "/appointments",
        json={
            "lead_id": lead["id"],
            "service_id": services["consulta"]["id"],
            "location_id": locations["sede_a"]["id"],
            "practitioner_id": practitioners["ana"]["id"],
            "start": slot_a["start"],
            "duration_minutes": 15,
            "end": _iso(_parse(slot_a["start"]) + timedelta(minutes=15)),
            "state": "cancelled",
        },
    )
    assert override.status_code == 422
    assert override.json()["error"]["code"] == "INVALID_INPUT"

    # booked interval (and every 45-min candidate overlapping it) is dropped:
    # a 45-min reservation excludes the 3 grid starts it intersects.
    after_book = _query_slots(client, services["consulta"]["id"], locations["sede_a"]["id"])
    expected_after_book = [iv for iv in expected_all if _disjoint(iv, interval_a)]
    after_book_intervals = [(_parse(slot["start"]), _parse(slot["end"])) for slot in after_book]
    assert after_book_intervals == expected_after_book
    assert interval_a not in after_book_intervals

    # --- step 8. reschedule to a different API-offered slot, same row --------
    slot_b = after_book[0]
    interval_b = (_parse(slot_b["start"]), _parse(slot_b["end"]))
    assert interval_b != interval_a

    reschedule_override = client.post(
        f"/appointments/{appointment_id}/reschedule",
        json={"new_start": slot_b["start"], "state": "cancelled"},
    )
    assert reschedule_override.status_code == 422
    assert reschedule_override.json()["error"]["code"] == "INVALID_INPUT"

    rescheduled = client.post(
        f"/appointments/{appointment_id}/reschedule",
        json={"new_start": slot_b["start"]},
    )
    assert rescheduled.status_code == 200, rescheduled.text
    res_body = rescheduled.json()
    assert res_body["id"] == appointment_id
    assert res_body["state"] == "confirmed"
    assert res_body["start_utc"] == slot_b["start"]
    assert _parse(res_body["end_utc"]) - _parse(res_body["start_utc"]) == timedelta(minutes=DURATION_MINUTES)

    # previous interval free again, new interval occupied, still one row
    after_resched = _query_slots(client, services["consulta"]["id"], locations["sede_a"]["id"])
    expected_after_resched = [iv for iv in expected_all if _disjoint(iv, interval_b)]
    after_resched_intervals = [
        (_parse(slot["start"]), _parse(slot["end"])) for slot in after_resched
    ]
    assert after_resched_intervals == expected_after_resched
    assert interval_a in after_resched_intervals
    assert interval_b not in after_resched_intervals

    # --- step 9. cancel: same row, state preserved, interval freed -----------
    cancelled = client.post(f"/appointments/{appointment_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    cancel_body = cancelled.json()
    assert cancel_body["id"] == appointment_id
    assert cancel_body["state"] == "cancelled"
    assert cancel_body["start_utc"] == slot_b["start"]
    assert cancel_body["end_utc"] == res_body["end_utc"]

    after_cancel = _query_slots(client, services["consulta"]["id"], locations["sede_a"]["id"])
    after_cancel_intervals = [
        (_parse(slot["start"]), _parse(slot["end"])) for slot in after_cancel
    ]
    assert after_cancel_intervals == expected_all
    assert interval_b in after_cancel_intervals
    assert interval_a in after_cancel_intervals

    # --- step 11. final DB verification (direct reads only) ------------------
    assert session.scalar(select(func.count()).select_from(Lead)) == 1
    assert session.scalar(select(func.count()).select_from(Service)) == 2
    assert session.scalar(select(func.count()).select_from(Location)) == 2
    assert session.scalar(select(func.count()).select_from(Practitioner)) == 2
    assert session.scalar(select(func.count()).select_from(PractitionerCapability)) == 2

    appointments = list(session.scalars(select(Appointment).order_by(Appointment.id)))
    assert len(appointments) == 1
    row = appointments[0]
    assert row.id == appointment_id
    assert row.state == "cancelled"
    assert row.start_utc == _parse(slot_b["start"])
    assert row.end_utc == _parse(res_body["end_utc"])
    # No duplicate confirmed reservation ever existed for the journey.
    confirmed = session.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.state == "confirmed")
    )
    assert confirmed == 0

    # --- step 12. coherent audit lifecycle -----------------------------------
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "appointment",
                AuditEvent.entity_id == str(appointment_id),
            )
            .order_by(AuditEvent.id)
        )
    )
    assert [event.action for event in events] == [
        "appointment.created",
        "appointment.rescheduled",
        "appointment.cancelled",
    ]
    created, resched, canc = events
    assert created.before_state is None
    assert created.after_state["state"] == "confirmed"
    assert _parse(created.after_state["start_utc"]) == _parse(slot_a["start"])
    assert _parse(resched.before_state["start_utc"]) == _parse(slot_a["start"])
    assert _parse(resched.after_state["start_utc"]) == _parse(slot_b["start"])
    assert resched.after_state["state"] == "confirmed"
    assert canc.before_state["state"] == "confirmed"
    assert _parse(canc.before_state["start_utc"]) == _parse(slot_b["start"])
    assert canc.after_state["state"] == "cancelled"
    # And nothing else was ever recorded for this appointment.
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.entity_id == str(appointment_id))
        )
        == 3
    )


def test_negative_capability_missing_and_missing_resource_envelope(client, session):
    ids = _seed(client)
    services = ids["services"]
    locations = ids["locations"]
    practitioners = ids["practitioners"]
    lead = ids["lead"]
    window_start, _ = _window()

    # Dra. Ana is deliberately NOT capable of Evaluacion Inicial @ Sede A.
    response = client.post(
        "/appointments",
        json={
            "lead_id": lead["id"],
            "service_id": services["eval"]["id"],
            "location_id": locations["sede_a"]["id"],
            "practitioner_id": practitioners["ana"]["id"],
            "start": window_start,
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "details"}
    assert body["error"]["code"] == "CAPABILITY_MISSING"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert body["error"]["details"] == {}

    # A missing appointment id fails both lifecycle verbs with the same envelope.
    for path, payload in (
        (f"/appointments/999999/reschedule", {"new_start": window_start}),
        (f"/appointments/999999/cancel", {}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 404, response.text
        body = response.json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) == {"code", "message", "details"}
        assert body["error"]["code"] == "NOT_FOUND"
        assert body["error"]["details"] == {}

    # The failed booking left no partial state behind.
    assert session.scalar(select(func.count()).select_from(Appointment)) == 0
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
