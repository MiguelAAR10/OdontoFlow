from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.catalog.schemas import ServiceCreate, ServiceRead
from app.catalog.service import create_service, list_services
from app.context import resolve_http_context
from app.db import get_db

router = APIRouter()


@router.post("/services", response_model=ServiceRead, status_code=201)
def create_service_route(
    payload: ServiceCreate, request: Request, db: Session = Depends(get_db)
) -> ServiceRead:
    ctx = resolve_http_context(request)
    return create_service(db, payload, ctx=ctx)


@router.get("/services", response_model=list[ServiceRead])
def list_services_route(
    request: Request, db: Session = Depends(get_db)
) -> list[ServiceRead]:
    # The tenant comes from the authenticated context. Omitting it fell back to
    # the bootstrap organization, so any credential read organization 1.
    ctx = resolve_http_context(request)
    return list_services(db, ctx=ctx)
