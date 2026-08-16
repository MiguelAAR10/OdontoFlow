"""PF closure — permission enforcement beyond scheduling (BLOCKER-2 included).

Proves that every remaining mutating surface is permission-gated with the
explicit context: catalog services, organization locations/practitioners/
capabilities, commercial leads (org-wide only — BLOCKER-2 resolution), and
scheduling availability config. Deny-by-default with location-scoped grants
honored (F-4/E4/E5).
"""

from __future__ import annotations

from datetime import date, time, timezone

import pytest

from app.catalog.schemas import ServiceCreate
from app.catalog.service import create_service
from app.commercial.schemas import LeadCreate
from app.commercial.service import create_lead
from app.errors import AppError
from app.iam.context import ExecutionContext
from app.iam.permissions import (
    APPOINTMENTS_CREATE,
    AVAILABILITY_MANAGE,
    CAPABILITIES_MANAGE,
    LEADS_CREATE,
    LOCATIONS_MANAGE,
    PRACTITIONERS_MANAGE,
    SERVICES_MANAGE,
)
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
)
from app.organization.schemas import CapabilityCreate, LocationCreate, PractitionerCreate
from app.organization.service import (
    create_capability,
    create_location,
    create_practitioner,
)
from app.scheduling.query import create_availability_rule, create_schedule_block
from app.scheduling.schemas import AvailabilityRuleCreate, ScheduleBlockCreate
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = "America/Lima"
MONDAY = date(2026, 8, 10)


def seed_actor(session, *, codes=(), location_id=None):
    principal = create_principal(session, display_name="actor", principal_type="human")
    membership = add_membership(session, organization_id=ORG, principal_id=principal.id)
    role = create_role(
        session, organization_id=ORG, code=f"role-{principal.id}", name="actor"
    )
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(
        session,
        organization_id=ORG,
        membership_id=membership.id,
        role_id=role.id,
        location_id=location_id,
    )
    values = principal.id
    session.rollback()
    return values


def ctx_for(principal_id):
    return ExecutionContext(
        organization_id=ORG,
        principal_id=principal_id,
        principal_type="human",
        request_id="req-pfclose",
        correlation_id="corr-pfclose",
    )


def test_create_lead_requires_org_wide_grant(session):
    """BLOCKER-2 resolution: a Lead has no location dimension, so creating one
    is an organization-wide operation — only an org-wide grant satisfies it
    (E5); a location-scoped grant can never create leads."""
    from app.organization.models import Location

    location = Location(organization_id=ORG, name="Sede", timezone=LIMA, is_active=True)
    session.add(location)
    session.commit()

    org_wide = seed_actor(session, codes=(LEADS_CREATE,))
    location_scoped = seed_actor(
        session, codes=(LEADS_CREATE,), location_id=location.id
    )

    create_lead(
        session,
        LeadCreate(full_name="Juan", contact_phone="+51999000001", acquisition_source="direct"),
        ctx=ctx_for(org_wide),
    )

    with pytest.raises(AppError) as exc:
        create_lead(
            session,
            LeadCreate(full_name="Ana", contact_phone="+51999000002", acquisition_source="direct"),
            ctx=ctx_for(location_scoped),
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_lead(
            session,
            LeadCreate(full_name="Sin permiso", contact_phone="+51999000003", acquisition_source="direct"),
            ctx=ctx_for(seed_actor(session, codes=())),
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()


def test_catalog_service_creation_is_permission_gated(session):
    no_perm = seed_actor(session, codes=())
    with pytest.raises(AppError) as exc:
        create_service(
            session,
            ServiceCreate(name="Limpieza", duration_minutes=30),
            ctx=ctx_for(no_perm),
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    manager = seed_actor(session, codes=(SERVICES_MANAGE,))
    service = create_service(
        session,
        ServiceCreate(name="Limpieza", duration_minutes=30),
        ctx=ctx_for(manager),
    )
    assert service.id is not None


def test_organization_mutations_are_permission_gated(session):
    no_perm = seed_actor(session, codes=())
    ctx = ctx_for(no_perm)

    with pytest.raises(AppError) as exc:
        create_location(session, LocationCreate(name="Sede", timezone=LIMA), ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_practitioner(session, PractitionerCreate(display_name="Dra. X"), ctx=ctx)
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_capability(
            session,
            CapabilityCreate(practitioner_id=1, service_id=1, location_id=1),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_availability_rule(
            session,
            AvailabilityRuleCreate(
                practitioner_id=1,
                location_id=1,
                day_of_week=0,
                start_local=time(9, 0),
                end_local=time(13, 0),
            ),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()

    with pytest.raises(AppError) as exc:
        create_schedule_block(
            session,
            ScheduleBlockCreate(
                practitioner_id=1,
                location_id=1,
                start_utc=MONDAY,
                end_utc=MONDAY,
            ),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()


def test_location_scoped_grants_are_honored(session):
    """A location-scoped manage grant works for its location and fails for
    another (F-4/E4)."""
    from app.organization.models import (
        Location,
        Practitioner,
        PractitionerMembership,
    )

    location_a = Location(organization_id=ORG, name="Sede A", timezone=LIMA, is_active=True)
    location_b = Location(organization_id=ORG, name="Sede B", timezone=LIMA, is_active=True)
    practitioner = Practitioner(display_name="Dra. Ana", is_active=True)
    session.add_all([location_a, location_b, practitioner])
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=ORG, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.commit()

    scoped = seed_actor(session, codes=(AVAILABILITY_MANAGE,), location_id=location_a.id)
    ctx = ctx_for(scoped)

    create_availability_rule(
        session,
        AvailabilityRuleCreate(
            practitioner_id=practitioner.id,
            location_id=location_a.id,
            day_of_week=0,
            start_local=time(9, 0),
            end_local=time(13, 0),
        ),
        ctx=ctx,
    )

    with pytest.raises(AppError) as exc:
        create_availability_rule(
            session,
            AvailabilityRuleCreate(
                practitioner_id=practitioner.id,
                location_id=location_b.id,
                day_of_week=0,
                start_local=time(9, 0),
                end_local=time(13, 0),
            ),
            ctx=ctx,
        )
    assert exc.value.code.value == "PERMISSION_DENIED"
    session.rollback()
