"""The fictitious reception catalog loader is complete and idempotent."""

from datetime import date

from sqlalchemy import func, select

from app.catalog.models import Promotion, Service
from app.organization.models import Location, Organization, Practitioner
from app.scheduling.models import AvailabilityRule
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.seed_reception_demo import PRACTITIONERS, seed_reception_demo


def test_seed_reception_demo_is_complete_current_and_idempotent(session):
    first = seed_reception_demo(
        session, organization_id=ORG, promotion_as_of=date(2026, 8, 25)
    )
    session.commit()
    second = seed_reception_demo(
        session, organization_id=ORG, promotion_as_of=date(2026, 8, 25)
    )
    session.commit()

    assert first == second == {
        "locations": 3,
        "services": 25,
        "promotions": 5,
        "practitioners": 5,
    }
    assert session.get(Organization, ORG).name == "ODONTO SMART"
    assert session.scalar(
        select(func.count()).select_from(Location).where(Location.name.like("ODONTO SMART%"))
    ) == 3
    assert session.scalar(
        select(func.count()).select_from(Service).where(Service.base_price.is_not(None))
    ) == 25
    assert session.scalar(select(func.count()).select_from(Promotion)) == 5
    assert session.scalar(
        select(func.count()).select_from(Practitioner).where(
            Practitioner.display_name.in_(
                [row[0] for row in PRACTITIONERS]
            )
        )
    ) == 5
    assert session.scalar(select(func.count()).select_from(AvailabilityRule)) == 30

