from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.catalog.models import Service
from app.errors import AppError, ErrorCode
from app.organization.models import (
    Location,
    Organization,
    Practitioner,
    PractitionerCapability,
    PractitionerMembership,
)
from app.organization.schemas import CapabilityCreate, LocationCreate, PractitionerCreate
from app.tenancy import resolve_organization_id, scoped

ORGANIZATION_ENTITY_TYPE = "organization"
ORGANIZATION_CREATED_ACTION = "organization.created"


def _validate_timezone(timezone: str) -> str:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise AppError(ErrorCode.INVALID_INPUT, f"Unknown IANA timezone: {timezone}.")
    return timezone


def _load_scoped(session: Session, model, entity_id: int, organization_id: int, label: str):
    """Load one tenant-owned row, or raise ``NOT_FOUND``.

    Another organization's row is indistinguishable from a non-existent one: the
    tenant filter is applied in the query, never after the read (§7.4).
    """
    entity = session.scalar(
        scoped(select(model).where(model.id == entity_id), model, organization_id)
    )
    if entity is None:
        raise AppError(ErrorCode.NOT_FOUND, f"{label} not found.")
    return entity


def load_membership(
    session: Session, practitioner_id: int, organization_id: int
) -> PractitionerMembership:
    """Resolve a practitioner through this organization's membership only (T5).

    A practitioner who does not work for the organization is reported exactly
    like an unknown one — the global ``practitioners`` table is never a tenant
    read surface.
    """
    membership = session.scalar(
        select(PractitionerMembership).where(
            PractitionerMembership.organization_id == organization_id,
            PractitionerMembership.practitioner_id == practitioner_id,
        )
    )
    if membership is None:
        raise AppError(ErrorCode.NOT_FOUND, "Practitioner not found.")
    return membership


def create_organization(session: Session, name: str) -> Organization:
    """Create one tenant root and audit it against its own id (PF0 D7)."""
    organization = Organization(name=name)
    session.add(organization)
    session.flush()
    record_event(
        session,
        organization_id=organization.id,
        entity_type=ORGANIZATION_ENTITY_TYPE,
        entity_id=str(organization.id),
        action=ORGANIZATION_CREATED_ACTION,
        after_state={"id": organization.id, "name": organization.name},
    )
    session.commit()
    session.refresh(organization)
    return organization


def create_location(
    session: Session, data: LocationCreate, organization_id: int | None = None
) -> Location:
    org_id = resolve_organization_id(organization_id)
    _validate_timezone(data.timezone)
    location = Location(
        organization_id=org_id, name=data.name, timezone=data.timezone, is_active=True
    )
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


def create_practitioner(
    session: Session, data: PractitionerCreate, organization_id: int | None = None
) -> Practitioner:
    """Register a global practitioner identity and onboard it into one org.

    The ``practitioners`` row stays global (P4/T2); the membership row is what
    makes the professional usable inside the acting organization (PM2).
    """
    org_id = resolve_organization_id(organization_id)
    practitioner = Practitioner(display_name=data.display_name, is_active=data.is_active)
    session.add(practitioner)
    session.flush()
    session.add(
        PractitionerMembership(
            organization_id=org_id, practitioner_id=practitioner.id, is_active=True
        )
    )
    session.commit()
    session.refresh(practitioner)
    return practitioner


def add_practitioner_membership(
    session: Session, practitioner_id: int, organization_id: int
) -> PractitionerMembership:
    """Grant an existing global practitioner access to a second organization."""
    if session.get(Practitioner, practitioner_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, "Practitioner not found.")
    membership = PractitionerMembership(
        organization_id=organization_id, practitioner_id=practitioner_id, is_active=True
    )
    session.add(membership)
    session.commit()
    session.refresh(membership)
    return membership


def create_capability(
    session: Session, data: CapabilityCreate, organization_id: int | None = None
) -> PractitionerCapability:
    """Declare that a member practitioner performs a service at a location.

    Every reference is resolved inside the acting organization, so a capability
    can never mix tenant resources. The composite FKs
    ``fk_capabilities_organization_{membership,service,location}`` are the final
    authority (§7.2).
    """
    org_id = resolve_organization_id(organization_id)
    load_membership(session, data.practitioner_id, org_id)
    _load_scoped(session, Service, data.service_id, org_id, "Service")
    _load_scoped(session, Location, data.location_id, org_id, "Location")
    capability = PractitionerCapability(
        organization_id=org_id,
        practitioner_id=data.practitioner_id,
        service_id=data.service_id,
        location_id=data.location_id,
        is_active=data.is_active,
    )
    session.add(capability)
    session.commit()
    session.refresh(capability)
    return capability


def list_eligible_practitioners(
    session: Session,
    service_id: int,
    location_id: int,
    organization_id: int | None = None,
) -> list[Practitioner]:
    org_id = resolve_organization_id(organization_id)
    service = _load_scoped(session, Service, service_id, org_id, "Service")
    location = _load_scoped(session, Location, location_id, org_id, "Location")
    if not service.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Service is inactive.")
    if not location.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Location is inactive.")

    # Both activity flags are required (PM3): the platform-wide
    # ``practitioners.is_active`` and the per-organization membership flag.
    statement = (
        select(Practitioner)
        .join(
            PractitionerMembership,
            PractitionerMembership.practitioner_id == Practitioner.id,
        )
        .join(
            PractitionerCapability,
            PractitionerCapability.practitioner_id == Practitioner.id,
        )
        .where(
            PractitionerMembership.organization_id == org_id,
            PractitionerMembership.is_active.is_(True),
            PractitionerCapability.organization_id == org_id,
            PractitionerCapability.service_id == service_id,
            PractitionerCapability.location_id == location_id,
            PractitionerCapability.is_active.is_(True),
            Practitioner.is_active.is_(True),
        )
        .order_by(Practitioner.display_name)
    )
    return list(session.scalars(statement))
