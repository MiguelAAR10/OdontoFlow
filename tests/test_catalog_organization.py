import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.catalog.models import Service
from app.catalog.schemas import ServiceCreate
from app.catalog.service import create_service, list_services
from app.errors import AppError, ErrorCode
from app.organization.models import Location, Practitioner, PractitionerCapability
from app.organization.schemas import CapabilityCreate, LocationCreate, PractitionerCreate
from app.organization.service import (
    create_capability,
    create_location,
    create_practitioner,
    list_eligible_practitioners,
)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_create_service_persists_valid_active(session):
    service = create_service(session, ServiceCreate(name="Limpieza", duration_minutes=30))
    assert service.id is not None
    assert service.duration_minutes == 30
    assert service.is_active is True
    assert session.get(Service, service.id) is not None
    assert _count(session, Service) == 1


def test_create_service_rejects_non_positive_duration(session):
    with pytest.raises(ValidationError):
        ServiceCreate(name="Invalido", duration_minutes=0)
    with pytest.raises(ValidationError):
        ServiceCreate(name="Invalido", duration_minutes=-10)
    assert _count(session, Service) == 0


def test_duplicate_service_name_rejected_safely(session):
    create_service(session, ServiceCreate(name="Limpieza", duration_minutes=30))
    with pytest.raises(AppError) as exc_info:
        create_service(session, ServiceCreate(name="Limpieza", duration_minutes=45))
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert _count(session, Service) == 1


def test_list_services_returns_all(session):
    create_service(session, ServiceCreate(name="Blanqueamiento", duration_minutes=60))
    create_service(session, ServiceCreate(name="Limpieza", duration_minutes=30))
    names = [s.name for s in list_services(session)]
    assert names == ["Blanqueamiento", "Limpieza"]


def test_location_valid_iana_timezone_accepted(session):
    for tz in ("America/Lima", "Europe/Madrid", "UTC", "America/Argentina/Buenos_Aires"):
        location = create_location(session, LocationCreate(name=f"Sede {tz}", timezone=tz))
        assert location.timezone == tz
    assert _count(session, Location) == 4


def test_location_invalid_timezone_rejected_before_persistence(session):
    with pytest.raises(AppError) as exc_info:
        create_location(session, LocationCreate(name="Sede X", timezone="Peru/Lima"))
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert _count(session, Location) == 0


@pytest.fixture
def eligibility(session):
    service_s = create_service(session, ServiceCreate(name="Limpieza", duration_minutes=30))
    service_t = create_service(session, ServiceCreate(name="Blanqueamiento", duration_minutes=60))
    location_x = create_location(session, LocationCreate(name="Sede Centro", timezone="America/Lima"))
    location_y = create_location(session, LocationCreate(name="Sede Norte", timezone="America/Lima"))
    practitioner_a = create_practitioner(session, PractitionerCreate(display_name="Dra. Ana"))
    practitioner_b = create_practitioner(session, PractitionerCreate(display_name="Dr. Luis"))
    return {
        "service_s": service_s,
        "service_t": service_t,
        "location_x": location_x,
        "location_y": location_y,
        "practitioner_a": practitioner_a,
        "practitioner_b": practitioner_b,
    }


def test_active_matching_capability_is_eligible(session, eligibility):
    e = eligibility
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=e["practitioner_a"].id,
            service_id=e["service_s"].id,
            location_id=e["location_x"].id,
        ),
    )
    result = list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert [p.id for p in result] == [e["practitioner_a"].id]


def test_capability_for_other_service_not_eligible(session, eligibility):
    e = eligibility
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=e["practitioner_b"].id,
            service_id=e["service_t"].id,
            location_id=e["location_x"].id,
        ),
    )
    result = list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert result == []


def test_capability_for_other_location_not_eligible(session, eligibility):
    e = eligibility
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=e["practitioner_a"].id,
            service_id=e["service_s"].id,
            location_id=e["location_y"].id,
        ),
    )
    result = list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert result == []


def test_inactive_capability_not_eligible(session, eligibility):
    e = eligibility
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=e["practitioner_a"].id,
            service_id=e["service_s"].id,
            location_id=e["location_x"].id,
            is_active=False,
        ),
    )
    result = list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert result == []


def test_inactive_practitioner_excluded(session, eligibility):
    e = eligibility
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=e["practitioner_a"].id,
            service_id=e["service_s"].id,
            location_id=e["location_x"].id,
        ),
    )
    e["practitioner_a"].is_active = False
    session.commit()
    result = list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert result == []


def test_inactive_service_raises_entity_inactive(session, eligibility):
    e = eligibility
    e["service_s"].is_active = False
    session.commit()
    with pytest.raises(AppError) as exc_info:
        list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert exc_info.value.code == ErrorCode.ENTITY_INACTIVE


def test_inactive_location_raises_entity_inactive(session, eligibility):
    e = eligibility
    e["location_x"].is_active = False
    session.commit()
    with pytest.raises(AppError) as exc_info:
        list_eligible_practitioners(session, e["service_s"].id, e["location_x"].id)
    assert exc_info.value.code == ErrorCode.ENTITY_INACTIVE


def test_missing_service_raises_not_found(session, eligibility):
    e = eligibility
    with pytest.raises(AppError) as exc_info:
        list_eligible_practitioners(session, 999999, e["location_x"].id)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


def test_missing_location_raises_not_found(session, eligibility):
    e = eligibility
    with pytest.raises(AppError) as exc_info:
        list_eligible_practitioners(session, e["service_s"].id, 999999)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


def test_duplicate_capability_rejected_by_database(session, eligibility):
    e = eligibility
    create_capability(
        session,
        CapabilityCreate(
            practitioner_id=e["practitioner_a"].id,
            service_id=e["service_s"].id,
            location_id=e["location_x"].id,
        ),
    )
    with pytest.raises(IntegrityError):
        create_capability(
            session,
            CapabilityCreate(
                practitioner_id=e["practitioner_a"].id,
                service_id=e["service_s"].id,
                location_id=e["location_x"].id,
            ),
        )
    session.rollback()
    assert _count(session, PractitionerCapability) == 1


def test_capability_missing_reference_raises_not_found(session, eligibility):
    e = eligibility
    with pytest.raises(AppError) as exc_info:
        create_capability(
            session,
            CapabilityCreate(
                practitioner_id=e["practitioner_a"].id,
                service_id=999999,
                location_id=e["location_x"].id,
            ),
        )
    assert exc_info.value.code == ErrorCode.NOT_FOUND
