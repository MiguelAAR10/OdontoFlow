from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.commercial.schemas import LeadCreate, LeadRead
from app.commercial.service import create_lead, get_lead, list_leads
from app.context import resolve_http_context
from app.db import get_db

router = APIRouter()


@router.get("/leads", response_model=list[LeadRead])
def list_leads_route(
    request: Request,
    db: Session = Depends(get_db),
    search: str | None = None,
    commercial_status: str | None = None,
) -> list[LeadRead]:
    ctx = resolve_http_context(request)
    return list_leads(
        db, ctx=ctx, search=search, commercial_status=commercial_status
    )


@router.post("/leads", response_model=LeadRead, status_code=201)
def create_lead_route(
    payload: LeadCreate, request: Request, db: Session = Depends(get_db)
) -> LeadRead:
    ctx = resolve_http_context(request)
    return create_lead(db, payload, ctx=ctx)


@router.get("/leads/{lead_id}", response_model=LeadRead)
def get_lead_route(lead_id: int, db: Session = Depends(get_db)) -> LeadRead:
    return get_lead(db, lead_id)
