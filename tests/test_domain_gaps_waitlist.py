"""B0.5 §4 — minimal waitlist (spec 2026-10-01)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import pytest

from app.organization.service import create_organization
from test_domain_gaps_helpers import (  # noqa: F401  (``api`` is a fixture)
    ORG,
    api,
    domain_events,
    seed_booking,
)


def _payload(ids: dict, **overrides) -> dict:
    body = {
        "lead_id": ids["lead_id"],
        "service_id": ids["service_id"],
        "location_id": ids["location_id"],
        "earliest_date": "2026-10-05",
        "latest_date": "2026-10-20",
        "preferred_window": "morning",
        "notes": "Prefiere lunes",
    }
    body.update(overrides)
    return body


def test_create_list_and_cancel_a_waitlist_entry(api, session):
    ids = seed_booking(session, suffix="wl")

    created = api.post("/waitlist", json=_payload(ids))

    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["status"] == "open"
    assert entry["preferred_window"] == "morning"
    assert entry["patient_id"] is None
    assert entry["practitioner_id"] is None
    events = domain_events(session, "waitlist.created", str(entry["id"]))
    assert len(events) == 1
    assert events[0].aggregate_type == "waitlist_entry"

    listed = api.get("/waitlist", params={"status": "open"})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [entry["id"]]

    cancelled = api.post(f"/waitlist/{entry['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert api.get("/waitlist", params={"status": "open"}).json() == []

    again = api.post(f"/waitlist/{entry['id']}/cancel")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ENTITY_INACTIVE"


def test_defaults_and_optional_fields(api, session):
    ids = seed_booking(session, suffix="wl-defaults")
    body = _payload(ids)
    for field in ("location_id", "preferred_window", "notes"):
        body.pop(field)

    created = api.post("/waitlist", json=body)

    assert created.status_code == 201, created.text
    assert created.json()["preferred_window"] == "any"
    assert created.json()["location_id"] is None


def test_filters_by_location_and_service(api, session):
    first = seed_booking(session, suffix="wl-a")
    second = seed_booking(session, suffix="wl-b")
    a = api.post("/waitlist", json=_payload(first)).json()["id"]
    b = api.post("/waitlist", json=_payload(second)).json()["id"]

    by_location = api.get("/waitlist", params={"location_id": first["location_id"]}).json()
    by_service = api.get("/waitlist", params={"service_id": second["service_id"]}).json()
    everything = api.get("/waitlist").json()

    assert [row["id"] for row in by_location] == [a]
    assert [row["id"] for row in by_service] == [b]
    assert [row["id"] for row in everything] == [a, b]


@pytest.mark.parametrize(
    "overrides",
    [
        {"earliest_date": "2026-10-20", "latest_date": "2026-10-05"},
        {"preferred_window": "night"},
        {"status": "booked"},
        {"lead_id": None},
    ],
)
def test_invalid_payloads_are_rejected(api, session, overrides):
    ids = seed_booking(session, suffix="wl-invalid")

    response = api.post("/waitlist", json=_payload(ids, **overrides))

    assert response.status_code == 422, response.text
    assert api.get("/waitlist").json() == []


def test_database_checks_back_the_schema(session):
    ids = seed_booking(session, suffix="wl-db")
    statements = (
        "INSERT INTO waitlist_entries (organization_id, lead_id, service_id, earliest_date, latest_date) "
        "VALUES (:org, :lead, :service, '2026-10-20', '2026-10-05')",
        "INSERT INTO waitlist_entries (organization_id, lead_id, service_id, earliest_date, latest_date, "
        "preferred_window) VALUES (:org, :lead, :service, '2026-10-05', '2026-10-20', 'night')",
        "INSERT INTO waitlist_entries (organization_id, lead_id, service_id, earliest_date, latest_date, "
        "status) VALUES (:org, :lead, :service, '2026-10-05', '2026-10-20', 'waiting')",
    )
    for statement in statements:
        with pytest.raises(IntegrityError):
            session.execute(
                text(statement),
                {"org": ORG, "lead": ids["lead_id"], "service": ids["service_id"]},
            )
            session.flush()
        session.rollback()


def test_waitlist_is_tenant_isolated(api, session):
    org_b = create_organization(session, "Clínica Espera B").id
    session.commit()
    ids_b = seed_booking(session, organization_id=org_b, suffix="wl-b-org")
    ids = seed_booking(session, suffix="wl-own")

    foreign_lead = api.post("/waitlist", json=_payload(ids, lead_id=ids_b["lead_id"]))
    assert foreign_lead.status_code == 404, foreign_lead.text

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO waitlist_entries (organization_id, lead_id, service_id, earliest_date, "
                "latest_date) VALUES (:org, :lead, :service, '2026-10-05', '2026-10-20')"
            ),
            {"org": ORG, "lead": ids_b["lead_id"], "service": ids["service_id"]},
        )
        session.flush()
    session.rollback()

    foreign_entry = session.execute(
        text(
            "INSERT INTO waitlist_entries (organization_id, lead_id, service_id, earliest_date, "
            "latest_date) VALUES (:org, :lead, :service, '2026-10-05', '2026-10-20') RETURNING id"
        ),
        {"org": org_b, "lead": ids_b["lead_id"], "service": ids_b["service_id"]},
    ).scalar_one()
    session.commit()

    assert api.get("/waitlist").json() == []
    cancel = api.post(f"/waitlist/{foreign_entry}/cancel")
    assert cancel.status_code == 404
