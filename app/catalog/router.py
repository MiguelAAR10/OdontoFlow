from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.catalog.schemas import ServiceCreate, ServiceRead
from app.catalog.service import create_service, list_services
from app.db import get_db

router = APIRouter()


@router.post("/services", response_model=ServiceRead, status_code=201)
def create_service_route(
    payload: ServiceCreate, db: Session = Depends(get_db)
) -> ServiceRead:
    return create_service(db, payload)


@router.get("/services", response_model=list[ServiceRead])
def list_services_route(db: Session = Depends(get_db)) -> list[ServiceRead]:
    return list_services(db)
