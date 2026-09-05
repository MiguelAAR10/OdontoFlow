"""Task 8 — FastAPI HTTP contract integration tests.

Every test drives the real FastAPI app (``create_app``) through the Starlette
TestClient against real PostgreSQL, exercising the thin routers, the typed
Pydantic schemas, the single error envelope from ``app/errors.py`` and the
booking retry policy for SQLSTATE 40P01 (deadlock).
"""

import threading
import time as clock
from datetime import datetime, time, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.db import get_db
from app.scheduling.models import AvailabilityRule, ScheduleBlock
from app.scheduling.router import get_booking_operation
from app.scheduling.service import book_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
UTC = timezone.utc
WINDOW_START = "2026-08-10T00:00:00Z"
WINDOW_END = "2026-08-11T00:00:00Z"


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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

    app.state.auth_sessionmaker = maker
    return app, maker


@pytest.fixture
def client(api_app):
    app, _maker = api_app
    return TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)


def _seed(client, service_duration=30):
    """Create the full commercial->catalog->organization->availability chain via the API."""
    service = client.post(
        "/services", json={"name": "Limpieza dental", "duration_minutes": service_duration}
    )
    assert service.status_code == 201, service.text
    service = service.json()
    location = client.post("/locations", json={"name": "Sede Centro", "timezone": LIMA})
    assert location.status_code == 201, location.text
    location = location.json()
    practitioner = client.post("/practitioners", json={"display_name": "Dra. Ana"})
    assert practitioner.status_code == 201, practitioner.text
    practitioner = practitioner.json()
    capability = client.post(
        "/capabilities",
        json={
            "practitioner_id": practitioner["id"],
            "service_id": service["id"],
            "location_id": location["id"],
        },
    )
    assert capability.status_code == 201, capability.text
    lead = client.post(
        "/leads",
        json={
            "full_name": "Juan Pérez",
            "contact_phone": "+51999000111",
            "acquisition_source": "direct",
        },
    )
    assert lead.status_code == 201, lead.text
    lead = lead.json()
    rule = client.post(
        "/availability-rules",
        json={
            "practitioner_id": practitioner["id"],
            "location_id": location["id"],
            "day_of_week": 0,
            "start_local": "09:00",
            "end_local": "11:00",
        },
    )
    assert rule.status_code == 201, rule.text
    return {
        "lead_id": lead["id"],
        "service_id": service["id"],
        "location_id": location["id"],
        "practitioner_id": practitioner["id"],
    }


# --- 1-2. catalog -----------------------------------------------------------


def test_create_service_returns_201_with_typed_schema(client):
    response = client.post("/services", json={"name": "Limpieza", "duration_minutes": 30})

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body["name"] == "Limpieza"
    assert body["duration_minutes"] == 30
    assert body["is_active"] is True


def test_list_services_is_deterministic(client):
    client.post("/services", json={"name": "Blanqueamiento", "duration_minutes": 60})
    client.post("/services", json={"name": "Limpieza", "duration_minutes": 30})

    response = client.get("/services")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["Blanqueamiento", "Limpieza"]


# --- 3-7. organization ------------------------------------------------------


def test_create_location_returns_201(client):
    response = client.post("/locations", json={"name": "Sede Centro", "timezone": LIMA})

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body["name"] == "Sede Centro"
    assert body["timezone"] == LIMA
    assert body["is_active"] is True


def test_create_location_invalid_timezone_returns_422_invalid_input(client):
    response = client.post("/locations", json={"name": "Sede X", "timezone": "Peru/Lima"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert body["error"]["details"] == {}


def test_create_practitioner_returns_201(client):
    response = client.post("/practitioners", json={"display_name": "Dra. Ana"})

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body["display_name"] == "Dra. Ana"
    assert body["is_active"] is True


def test_create_capability_returns_201(client):
    service = client.post("/services", json={"name": "Limpieza", "duration_minutes": 30}).json()
    location = client.post("/locations", json={"name": "Sede Centro", "timezone": LIMA}).json()
    practitioner = client.post("/practitioners", json={"display_name": "Dra. Ana"}).json()

    response = client.post(
        "/capabilities",
        json={
            "practitioner_id": practitioner["id"],
            "service_id": service["id"],
            "location_id": location["id"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["practitioner_id"] == practitioner["id"]
    assert body["service_id"] == service["id"]
    assert body["location_id"] == location["id"]
    assert body["is_active"] is True


def test_eligible_practitioners_only_valid_ones(client):
    service = client.post("/services", json={"name": "Limpieza", "duration_minutes": 30}).json()
    location = client.post("/locations", json={"name": "Sede Centro", "timezone": LIMA}).json()
    ana = client.post("/practitioners", json={"display_name": "Dra. Ana"}).json()
    luis = client.post("/practitioners", json={"display_name": "Dr. Luis"}).json()
    client.post(
        "/capabilities",
        json={
            "practitioner_id": ana["id"],
            "service_id": service["id"],
            "location_id": location["id"],
        },
    )

    response = client.get(
        "/practitioners/eligible",
        params={"service_id": service["id"], "location_id": location["id"]},
    )

    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == [ana["id"]]


# --- 8-10. commercial -------------------------------------------------------


def test_create_lead_returns_201(client):
    response = client.post(
        "/leads",
        json={
            "full_name": "Juan Pérez",
            "contact_phone": "+51999000111",
            "acquisition_source": "direct",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body["full_name"] == "Juan Pérez"
    assert body["contact_phone"] == "+51999000111"
    assert body["acquisition_source"] == "direct"
    assert body["commercial_status"] == "new"


def test_get_lead_returns_correct_commercial_lead(client):
    created = client.post(
        "/leads",
        json={
            "full_name": "María López",
            "contact_email": "maria@example.com",
            "acquisition_source": "referral",
        },
    ).json()

    response = client.get(f"/leads/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["full_name"] == "María López"
    assert body["contact_email"] == "maria@example.com"
    assert body["contact_phone"] is None
    assert body["acquisition_source"] == "referral"
    assert body["commercial_status"] == "new"


def test_create_lead_invalid_source_returns_422_stable_envelope(client):
    response = client.post(
        "/leads",
        json={
            "full_name": "Ana",
            "contact_phone": "+51999000111",
            "acquisition_source": "friend",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert "detail" not in body
    assert "loc" not in body["error"]


def test_create_lead_without_contact_returns_422_stable_envelope(client):
    response = client.post(
        "/leads",
        json={"full_name": "Ana", "acquisition_source": "direct"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert body["error"]["details"] == {}


# --- 11-12. availability-rules and schedule-blocks --------------------------


def test_create_availability_rule_persists(client, session):
    ids = _seed(client)

    response = client.post(
        "/availability-rules",
        json={
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
            "day_of_week": 1,
            "start_local": "14:00",
            "end_local": "16:00",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["day_of_week"] == 1
    rule = session.scalar(
        select(AvailabilityRule).where(AvailabilityRule.day_of_week == 1)
    )
    assert rule is not None
    assert rule.practitioner_id == ids["practitioner_id"]
    assert rule.location_id == ids["location_id"]
    assert rule.start_local == time(14, 0)
    assert rule.end_local == time(16, 0)


def test_create_schedule_block_persists(client, session):
    ids = _seed(client)

    response = client.post(
        "/schedule-blocks",
        json={
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
            "start_utc": "2026-08-10T14:30:00Z",
            "end_utc": "2026-08-10T15:00:00Z",
        },
    )

    assert response.status_code == 201
    block = session.scalar(
        select(ScheduleBlock).where(
            ScheduleBlock.start_utc == datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
        )
    )
    assert block is not None
    assert block.practitioner_id == ids["practitioner_id"]
    assert block.location_id == ids["location_id"]
    assert block.end_utc == datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def test_create_availability_rule_rejects_inverted_interval(client):
    ids = _seed(client)

    response = client.post(
        "/availability-rules",
        json={
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
            "day_of_week": 1,
            "start_local": "16:00",
            "end_local": "14:00",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_create_availability_rule_missing_practitioner_returns_404(client):
    response = client.post(
        "/availability-rules",
        json={
            "practitioner_id": 999_999,
            "location_id": 999_999,
            "day_of_week": 0,
            "start_local": "09:00",
            "end_local": "11:00",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_create_schedule_block_rejects_inverted_or_naive_interval(client):
    ids = _seed(client)
    for payload in (
        {"start_utc": "2026-08-10T15:00:00Z", "end_utc": "2026-08-10T14:00:00Z"},
        {"start_utc": "2026-08-10T14:00:00", "end_utc": "2026-08-10T15:00:00"},
    ):
        response = client.post(
            "/schedule-blocks",
            json={
                "practitioner_id": ids["practitioner_id"],
                "location_id": ids["location_id"],
                **payload,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "INVALID_INPUT"


# --- 13-15. slot query ------------------------------------------------------


def _query_slots(client, ids, window_start=WINDOW_START, window_end=WINDOW_END):
    return client.post(
        "/slots/query",
        json={
            "service_id": ids["service_id"],
            "location_id": ids["location_id"],
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def _slot_starts(slots):
    return [_dt(slot["start"]) for slot in slots]


def test_slots_query_returns_deterministic_valid_slots(client):
    ids = _seed(client)

    response = _query_slots(client, ids)

    assert response.status_code == 200
    slots = response.json()
    expected = [
        (datetime(2026, 8, 10, 14, 0, tzinfo=UTC), datetime(2026, 8, 10, 14, 30, tzinfo=UTC)),
        (datetime(2026, 8, 10, 14, 15, tzinfo=UTC), datetime(2026, 8, 10, 14, 45, tzinfo=UTC)),
        (datetime(2026, 8, 10, 14, 30, tzinfo=UTC), datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        (datetime(2026, 8, 10, 14, 45, tzinfo=UTC), datetime(2026, 8, 10, 15, 15, tzinfo=UTC)),
        (datetime(2026, 8, 10, 15, 0, tzinfo=UTC), datetime(2026, 8, 10, 15, 30, tzinfo=UTC)),
        (datetime(2026, 8, 10, 15, 15, tzinfo=UTC), datetime(2026, 8, 10, 15, 45, tzinfo=UTC)),
        (datetime(2026, 8, 10, 15, 30, tzinfo=UTC), datetime(2026, 8, 10, 16, 0, tzinfo=UTC)),
    ]
    got = [
        (_dt(slot["start"]), _dt(slot["end"]))
        for slot in slots
        if slot["practitioner_id"] == ids["practitioner_id"]
    ]
    assert got == expected
    assert got == sorted(got)


def test_slots_query_excludes_blocked_intervals(client):
    ids = _seed(client)
    client.post(
        "/schedule-blocks",
        json={
            "practitioner_id": ids["practitioner_id"],
            "location_id": ids["location_id"],
            "start_utc": "2026-08-10T14:30:00Z",
            "end_utc": "2026-08-10T15:00:00Z",
        },
    )

    response = _query_slots(client, ids)

    assert response.status_code == 200
    expected = [
        datetime(2026, 8, 10, h, m, tzinfo=UTC)
        for h, m in [(14, 0), (15, 0), (15, 15), (15, 30)]
    ]
    assert _slot_starts(response.json()) == expected


def test_slots_query_excludes_confirmed_appointment_intervals(client):
    ids = _seed(client)
    booking = client.post(
        "/appointments",
        json={
            "lead_id": ids["lead_id"],
            "service_id": ids["service_id"],
            "location_id": ids["location_id"],
            "practitioner_id": ids["practitioner_id"],
            "start": "2026-08-10T14:30:00Z",
        },
    )
    assert booking.status_code == 201, booking.text

    response = _query_slots(client, ids)

    assert response.status_code == 200
    expected = [
        datetime(2026, 8, 10, h, m, tzinfo=UTC)
        for h, m in [(14, 0), (15, 0), (15, 15), (15, 30)]
    ]
    assert _slot_starts(response.json()) == expected


def test_slots_query_rejects_naive_window(client):
    ids = _seed(client)

    response = _query_slots(client, ids, window_end="2026-08-11T00:00:00")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_slots_query_rejects_inverted_window(client):
    ids = _seed(client)

    response = _query_slots(
        client, ids, window_start=WINDOW_END, window_end=WINDOW_START
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_slots_query_missing_service_returns_404(client):
    ids = _seed(client)

    response = client.post(
        "/slots/query",
        json={
            "service_id": 999_999,
            "location_id": ids["location_id"],
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- 16-19. booking ---------------------------------------------------------


def _book(client, ids, start="2026-08-10T14:00:00Z", **extra):
    payload = {
        "lead_id": ids["lead_id"],
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "practitioner_id": ids["practitioner_id"],
        "start": start,
    }
    payload.update(extra)
    return client.post("/appointments", json=payload)


def test_create_appointment_returns_201_confirmed(client):
    ids = _seed(client)

    response = _book(client, ids)

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "confirmed"
    assert body["lead_id"] == ids["lead_id"]
    assert body["service_id"] == ids["service_id"]
    assert body["location_id"] == ids["location_id"]
    assert body["practitioner_id"] == ids["practitioner_id"]
    assert _dt(body["start_utc"]) == datetime(2026, 8, 10, 14, 0, tzinfo=UTC)


def test_booking_response_end_reflects_canonical_service_duration(client):
    ids = _seed(client, service_duration=45)

    response = _book(client, ids)

    assert response.status_code == 201
    body = response.json()
    assert _dt(body["start_utc"]) == datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    assert _dt(body["end_utc"]) == datetime(2026, 8, 10, 14, 45, tzinfo=UTC)


def test_client_cannot_override_duration_end_or_state(client):
    ids = _seed(client, service_duration=45)

    response = _book(
        client,
        ids,
        duration_minutes=15,
        end="2026-08-10T14:15:00Z",
        state="cancelled",
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"


def test_booking_missing_reference_returns_404(client):
    ids = _seed(client)

    response = client.post(
        "/appointments",
        json={
            "lead_id": ids["lead_id"],
            "service_id": 999_999,
            "location_id": ids["location_id"],
            "practitioner_id": ids["practitioner_id"],
            "start": "2026-08-10T14:00:00Z",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_booking_inactive_service_returns_409_entity_inactive(client, session):
    ids = _seed(client)
    from app.catalog.models import Service

    service = session.get(Service, ids["service_id"])
    service.is_active = False
    session.commit()

    response = _book(client, ids)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ENTITY_INACTIVE"


# --- 20. real 23P01 path ----------------------------------------------------


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


def test_real_23p01_path_returns_409_appointment_conflict(api_app, session):
    """The GiST exclusion — not the preflight — rejects the late booker.

    The HTTP booking request is gated at its INSERT *after* a preflight that
    saw a free slot; the winning row is committed underneath it. The resulting
    IntegrityError (23P01) must surface through the router untouched so the
    transport layer maps it to the stable 409 APPOINTMENT_CONFLICT envelope.
    """
    app, maker = api_app
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)
    ids = _seed(client)

    gate = maker()
    gate.execute(text("LOCK TABLE appointments IN EXCLUSIVE MODE"))
    outcome = {}

    def attempt():
        try:
            response = _book(client, ids)
            outcome["status"] = response.status_code
            outcome["body"] = response.json()
            outcome["text"] = response.text
        except Exception as exc:  # pragma: no cover - defensive
            outcome["error"] = exc

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
                "org": ORG,
                "lead": ids["lead_id"],
                "service": ids["service_id"],
                "practitioner": ids["practitioner_id"],
                "location": ids["location_id"],
                "start": datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
                "end": datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
            },
        )
    finally:
        gate.commit()
        gate.close()
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert blocked_after_preflight, outcome
    assert outcome["status"] == 409, outcome
    assert outcome["body"]["error"]["code"] == "APPOINTMENT_CONFLICT"
    assert "23P01" not in outcome["text"]
    assert "conflicting key" not in outcome["text"]
    assert "excl_appointments_confirmed_no_overlap" not in outcome["text"]

    rows = session.execute(text("SELECT id FROM appointments ORDER BY id")).all()
    assert len(rows) == 1
    audit_count = session.execute(text("SELECT count(*) FROM audit_events")).scalar()
    assert audit_count == 0


# --- 21-23. 40P01 retry policy (booking-specific seam) ----------------------


class _Fake40P01:
    sqlstate = "40P01"


def _fake_40p01_exc():
    return OperationalError("SELECT 1", {}, _Fake40P01())


def test_first_40p01_retries_exactly_once_then_succeeds(api_app):
    app, _maker = api_app
    calls = []

    def fake_op(session, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _fake_40p01_exc()
        return {
            "id": 777,
            "lead_id": kwargs["lead_id"],
            "service_id": kwargs["service_id"],
            "practitioner_id": kwargs["practitioner_id"],
            "location_id": kwargs["location_id"],
            "start_utc": datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
            "end_utc": datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
            "state": "confirmed",
        }

    app.dependency_overrides[get_booking_operation] = lambda: fake_op
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)

    response = client.post(
        "/appointments",
        json={
            "lead_id": 1,
            "service_id": 1,
            "location_id": 1,
            "practitioner_id": 1,
            "start": "2026-08-10T14:00:00Z",
        },
    )

    assert response.status_code == 201
    assert response.json()["state"] == "confirmed"
    assert len(calls) == 2


def test_40p01_once_then_success_returns_201(api_app):
    app, _maker = api_app
    calls = []
    real_operation = book_appointment

    def fake_op(session, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _fake_40p01_exc()
        return real_operation(session, **kwargs)

    app.dependency_overrides[get_booking_operation] = lambda: fake_op
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)
    ids = _seed(client)

    response = _book(client, ids)

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "confirmed"
    assert len(calls) == 2
    assert _dt(body["start_utc"]) == datetime(2026, 8, 10, 14, 0, tzinfo=UTC)


def test_repeated_40p01_returns_409_appointment_conflict_without_db_leaks(api_app):
    app, _maker = api_app
    calls = []

    def fake_op(session, **kwargs):
        calls.append(1)
        raise _fake_40p01_exc()

    app.dependency_overrides[get_booking_operation] = lambda: fake_op
    client = TestClient(app, raise_server_exceptions=False, headers=AUTH_HEADERS)

    response = client.post(
        "/appointments",
        json={
            "lead_id": 1,
            "service_id": 1,
            "location_id": 1,
            "practitioner_id": 1,
            "start": "2026-08-10T14:00:00Z",
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "APPOINTMENT_CONFLICT"
    assert "40P01" not in response.text
    assert "deadlock" not in response.text
    assert "Traceback" not in response.text
    assert len(calls) == 2


# --- 24-25. OpenAPI and health ----------------------------------------------


def test_openapi_exposes_all_routes_and_typed_schemas(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    for path in (
        "/health",
        "/leads",
        "/leads/{lead_id}",
        "/services",
        "/locations",
        "/practitioners",
        "/capabilities",
        "/practitioners/eligible",
        "/availability-rules",
        "/schedule-blocks",
        "/slots/query",
        "/appointments",
    ):
        assert path in spec["paths"], path

    paths = spec["paths"]
    assert "post" in paths["/appointments"]
    assert "post" in paths["/slots/query"]
    assert "get" in paths["/services"]
    assert "get" in paths["/leads/{lead_id}"]

    schemas = spec["components"]["schemas"]
    for name in (
        "ServiceCreate",
        "ServiceRead",
        "LeadCreate",
        "LeadRead",
        "LocationCreate",
        "LocationRead",
        "PractitionerCreate",
        "PractitionerRead",
        "CapabilityCreate",
        "CapabilityRead",
        "AvailabilityRuleCreate",
        "AvailabilityRuleRead",
        "ScheduleBlockCreate",
        "ScheduleBlockRead",
        "SlotQuery",
        "SlotResult",
        "AppointmentCreate",
        "AppointmentRead",
    ):
        assert name in schemas, name


def test_health_unchanged(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
