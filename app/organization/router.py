from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.context import resolve_http_context
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
    list_locations,
)

router = APIRouter()


@router.get("/locations", response_model=list[LocationRead])
def list_locations_route(
    request: Request, db: Session = Depends(get_db)
) -> list[LocationRead]:
    ctx = resolve_http_context(request)
    return list_locations(db, ctx=ctx)


@router.post("/locations", response_model=LocationRead, status_code=201)
def create_location_route(
    payload: LocationCreate, request: Request, db: Session = Depends(get_db)
) -> LocationRead:
    ctx = resolve_http_context(request)
    return create_location(db, payload, ctx=ctx)


@router.post("/practitioners", response_model=PractitionerRead, status_code=201)
def create_practitioner_route(
    payload: PractitionerCreate, request: Request, db: Session = Depends(get_db)
) -> PractitionerRead:
    ctx = resolve_http_context(request)
    return create_practitioner(db, payload, ctx=ctx)


@router.post("/capabilities", response_model=CapabilityRead, status_code=201)
def create_capability_route(
    payload: CapabilityCreate, request: Request, db: Session = Depends(get_db)
) -> CapabilityRead:
    ctx = resolve_http_context(request)
    return create_capability(db, payload, ctx=ctx)


@router.get("/practitioners/eligible", response_model=list[PractitionerRead])
def list_eligible_practitioners_route(
    service_id: int, location_id: int, db: Session = Depends(get_db)
) -> list[PractitionerRead]:
    return list_eligible_practitioners(db, service_id, location_id)
