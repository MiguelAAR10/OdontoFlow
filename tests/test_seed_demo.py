"""B0 demo seed: deterministic, idempotent, and safe to point at a database."""

from __future__ import annotations

import stat
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.clinical.models import Patient, ServiceExecution, Visit
from app.db import get_db
from app.economics.models import Charge, Payment, Product
from app.inventory.service import get_balance
from app.organization.models import Location
from app.scheduling.models import Appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import PROFILE_PERMISSIONS
from scripts.seed_demo import (
    LOW_STOCK_PRODUCT,
    OVERDUE_MIN_AGE_DAYS,
    assert_local_database_url,
    issue_staff_credential,
    main,
    seed_demo,
)

ANCHOR = date(2026, 10, 1)
LIMA = ZoneInfo("America/Lima")


def _snapshot(session) -> dict:
    session.expire_all()
    rows = {
        "patients": session.scalar(select(func.count()).select_from(Patient)),
        "appointments": session.scalar(select(func.count()).select_from(Appointment)),
        "visits": session.scalar(select(func.count()).select_from(Visit)),
        "executions": session.scalar(select(func.count()).select_from(ServiceExecution)),
        "charges": session.scalar(select(func.count()).select_from(Charge)),
        "payments": session.scalar(select(func.count()).select_from(Payment)),
        "products": session.scalar(select(func.count()).select_from(Product)),
        "appointment_starts": tuple(
            session.scalars(select(Appointment.start_utc).order_by(Appointment.start_utc))
        ),
        "charge_amounts": tuple(session.scalars(select(Charge.amount).order_by(Charge.id))),
    }
    session.commit()
    return rows


def _paid(session, charge_id: int) -> Decimal:
    return session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.charge_id == charge_id)
    )


def test_seed_demo_twice_yields_the_same_state(session):
    first_summary = seed_demo(session, organization_id=ORG, anchor=ANCHOR)
    first = _snapshot(session)
    second_summary = seed_demo(session, organization_id=ORG, anchor=ANCHOR)
    second = _snapshot(session)

    assert first_summary == second_summary
    assert first == second
    assert first["patients"] == 40
    assert first_summary["patients"] == 40


def test_seed_demo_has_past_and_future_appointments_and_mixed_charges(session):
    summary = seed_demo(session, organization_id=ORG, anchor=ANCHOR)
    session.expire_all()
    anchor_start = ANCHOR
    starts = [
        value.astimezone(LIMA).date()
        for value in session.scalars(select(Appointment.start_utc))
    ]
    assert any(day < anchor_start for day in starts)
    assert any(day > anchor_start for day in starts)
    assert all(
        state == "confirmed" for state in session.scalars(select(Appointment.state))
    )

    charges = session.scalars(select(Charge)).all()
    paid, partial, overdue = [], [], []
    for charge in charges:
        amount_paid = _paid(session, charge.id)
        age = (ANCHOR - charge.created_at.astimezone(LIMA).date()).days
        if amount_paid == charge.amount:
            paid.append(charge)
        elif amount_paid > 0:
            partial.append(charge)
        if amount_paid < charge.amount and age >= OVERDUE_MIN_AGE_DAYS:
            overdue.append((charge, age))
    assert paid and partial
    assert len(overdue) >= 3
    assert any(
        charge.amount == Decimal("180.00") and age == 12 and _paid(session, charge.id) == 0
        for charge, age in overdue
    )
    assert summary["overdue_charges"] == len(overdue)
    session.commit()


def test_seed_demo_stock_is_low_in_one_location_and_ample_in_another(session):
    seed_demo(session, organization_id=ORG, anchor=ANCHOR)
    session.commit()
    product = session.scalar(select(Product).where(Product.name == LOW_STOCK_PRODUCT))
    lince = session.scalar(select(Location).where(Location.name == "ODONTO SMART Lince"))
    jesus_maria = session.scalar(
        select(Location).where(Location.name == "ODONTO SMART Jesús María")
    )
    session.commit()
    assert product is not None
    low = get_balance(session, product.id, location_id=lince.id, organization_id=ORG)
    session.commit()
    ample = get_balance(session, product.id, location_id=jesus_maria.id, organization_id=ORG)
    session.commit()
    assert low < Decimal("10")
    assert ample >= Decimal("50")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
        "postgresql+psycopg://u:p@10.0.0.5:5432/odontoflow",
        "postgresql+psycopg://u:p@db.example.com/odontoflow",
    ],
)
def test_remote_database_urls_are_refused_without_the_explicit_flag(url):
    with pytest.raises(SystemExit):
        assert_local_database_url(url)
    assert_local_database_url(url, allow_remote=True)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@127.0.0.1:5434/odontoflow",
        "postgresql+psycopg://u:p@localhost:5434/odontoflow",
    ],
)
def test_local_database_urls_are_accepted(url):
    assert_local_database_url(url)


def test_main_refuses_a_remote_database_before_connecting(capsys):
    with pytest.raises(SystemExit):
        main(["--database-url", "postgresql+psycopg://u:p@db.example.com/x"])
    assert "p@db" not in capsys.readouterr().out


def test_staff_demo_profile_is_staff_only():
    permissions = set(PROFILE_PERMISSIONS["reception-staff-demo"])
    assert {
        "patients.read",
        "patients.create",
        "appointments.read",
        "appointments.create",
        "appointments.cancel",
        "appointments.reschedule",
        "visits.read",
        "visits.create",
        "executions.read",
        "executions.create",
        "leads.read",
        "leads.create",
        "locations.read",
        "services.read",
        "practitioners.read",
        "availability.read",
        "charges.read",
        "charges.create",
        "payments.read",
        "payments.create",
        "payments.manage",
        "follow_ups.read",
        "follow_ups.create",
        "follow_ups.manage",
        "products.read",
        "products.create",
        "movements.read",
        "movements.create",
    } <= permissions
    agent_only = {
        permission
        for permission in permissions
        if permission.startswith(("conversations.", "contact_", "deliveries.", "messages."))
    }
    assert agent_only == set()
    assert not {
        "locations.manage",
        "services.manage",
        "practitioners.manage",
        "availability.manage",
        "capabilities.manage",
    } & permissions


def test_issue_staff_credential_writes_a_private_env_file(
    session, migrated_engine, tmp_path, capsys
):
    env_path = tmp_path / ".env.demo.local"
    issue_staff_credential(session, organization_id=ORG, env_path=env_path)

    content = env_path.read_text(encoding="utf-8")
    assert content.startswith("BACKEND_DEMO_TOKEN=ofk_")
    token = content.strip().split("=", 1)[1]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert token not in out
    assert "escrito en .env.demo.local" in out

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
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/charges", headers=headers).status_code == 200
        assert client.get("/patients", headers=headers).status_code == 200
        forbidden = client.get("/internal/conversations", headers=headers)
    assert forbidden.status_code == 403, forbidden.text


def test_anchor_default_is_today_in_lima():
    from scripts.seed_demo import default_anchor

    today = default_anchor()
    assert isinstance(today, date)
    assert abs((today - date.today()).days) <= 1


@pytest.mark.parametrize("offset", range(7))
def test_key_overdue_charge_is_twelve_days_old_for_any_weekday(session, offset):
    """Whatever weekday ``anchor - 12`` falls on (Sunday included), the key
    S/ 180 charge is exactly 12 days old and still unpaid."""
    anchor = date(2026, 10, 1) + timedelta(days=offset)
    seed_demo(session, organization_id=ORG, anchor=anchor)
    session.expire_all()
    ages = [
        (anchor - charge.created_at.astimezone(LIMA).date()).days
        for charge in session.scalars(select(Charge).where(Charge.amount == Decimal("180.00")))
        if _paid(session, charge.id) == 0
    ]
    session.commit()
    assert 12 in ages
