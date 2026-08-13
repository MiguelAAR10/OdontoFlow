from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.errors import AppError, ErrorCode
from app.organization.models import Location, Practitioner, PractitionerCapability
from app.organization.schemas import CapabilityCreate, LocationCreate, PractitionerCreate


def _validate_timezone(timezone: str) -> str:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise AppError(ErrorCode.INVALID_INPUT, f"Unknown IANA timezone: {timezone}.")
    return timezone


def create_location(session: Session, data: LocationCreate) -> Location:
    _validate_timezone(data.timezone)
    location = Location(name=data.name, timezone=data.timezone, is_active=True)
    session.add(location)
    session.commit()
    session.refresh(location)
    return location


def create_practitioner(session: Session, data: PractitionerCreate) -> Practitioner:
    practitioner = Practitioner(display_name=data.display_name, is_active=data.is_active)
    session.add(practitioner)
    session.commit()
    session.refresh(practitioner)
    return practitioner


def create_capability(
    session: Session, data: CapabilityCreate
) -> PractitionerCapability:
    if session.get(Practitioner, data.practitioner_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, "Practitioner not found.")
    if session.get(Service, data.service_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, "Service not found.")
    if session.get(Location, data.location_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, "Location not found.")
    capability = PractitionerCapability(
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
    session: Session, service_id: int, location_id: int
) -> list[Practitioner]:
    service = session.get(Service, service_id)
    if service is None:
        raise AppError(ErrorCode.NOT_FOUND, "Service not found.")
    location = session.get(Location, location_id)
    if location is None:
        raise AppError(ErrorCode.NOT_FOUND, "Location not found.")
    if not service.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Service is inactive.")
    if not location.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Location is inactive.")

    statement = (
        select(Practitioner)
        .join(
            PractitionerCapability,
            PractitionerCapability.practitioner_id == Practitioner.id,
        )
        .where(
            PractitionerCapability.service_id == service_id,
            PractitionerCapability.location_id == location_id,
            PractitionerCapability.is_active.is_(True),
            Practitioner.is_active.is_(True),
        )
        .order_by(Practitioner.display_name)
    )
    return list(session.scalars(statement))
