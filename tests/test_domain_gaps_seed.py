"""B0.5 §9 — demo seed and staff profile pick up the new domain facts."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.context import default_context
from app.inventory.service import list_low_stock
from app.organization.models import Location
from app.scheduling.waitlist import WaitlistEntry
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import PROFILE_PERMISSIONS
from scripts.seed_demo import LOW_STOCK_PRODUCT, seed_demo

ANCHOR = date(2026, 10, 1)


def test_staff_demo_profile_gets_the_new_codes_except_reversal():
    permissions = set(PROFILE_PERMISSIONS["reception-staff-demo"])
    assert {
        "appointments.record_outcome",
        "reorder_points.manage",
        "waitlist.read",
        "waitlist.manage",
    } <= permissions
    # Reversal is L4 / human-only: the integration staff credential never holds it.
    assert "payments.reverse" not in permissions


def test_seed_demo_adds_waitlist_and_reorder_points_idempotently(session):
    seed_demo(session, organization_id=ORG, anchor=ANCHOR)
    seed_demo(session, organization_id=ORG, anchor=ANCHOR)

    open_entries = session.scalar(
        select(func.count()).select_from(WaitlistEntry).where(WaitlistEntry.status == "open")
    )
    lince = session.scalar(select(Location).where(Location.name == "ODONTO SMART Lince"))
    session.commit()
    assert open_entries == 3

    low = list_low_stock(session, ctx=default_context(ORG))
    session.rollback()
    low_pairs = {(row.product_name, row.location_id) for row in low}
    assert (LOW_STOCK_PRODUCT, lince.id) in low_pairs
    assert all(row.location_id == lince.id for row in low if row.product_name == LOW_STOCK_PRODUCT)
