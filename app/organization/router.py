from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.organization.schemas import (
    CapabilityCreate,
    CapabilityRead,
    LocationCreate,
    LocationRead,
    PractitionerCreate,
    PractitionerRead,
)
from app.organization.service import (
    create_capability,
    create_location,
    create_practitioner,
    list_eligible_practitioners,
)

router = APIRouter()


@router.post("/locations", response_model=LocationRead, status_code=201)
def create_location_route(
    payload: LocationCreate, db: Session = Depends(get_db)
) -> LocationRead:
    return create_location(db, payload)


@router.post("/practitioners", response_model=PractitionerRead, status_code=201)
def create_practitioner_route(
    payload: PractitionerCreate, db: Session = Depends(get_db)
) -> PractitionerRead:
    return create_practitioner(db, payload)


@router.post("/capabilities", response_model=CapabilityRead, status_code=201)
def create_capability_route(
    payload: CapabilityCreate, db: Session = Depends(get_db)
) -> CapabilityRead:
    return create_capability(db, payload)


@router.get("/practitioners/eligible", response_model=list[PractitionerRead])
def list_eligible_practitioners_route(
    service_id: int, location_id: int, db: Session = Depends(get_db)
) -> list[PractitionerRead]:
    return list_eligible_practitioners(db, service_id, location_id)
