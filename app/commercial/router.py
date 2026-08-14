from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.commercial.schemas import LeadCreate, LeadRead
from app.commercial.service import create_lead, get_lead
from app.db import get_db

router = APIRouter()


@router.post("/leads", response_model=LeadRead, status_code=201)
def create_lead_route(
    payload: LeadCreate, db: Session = Depends(get_db)
) -> LeadRead:
    return create_lead(db, payload)


@router.get("/leads/{lead_id}", response_model=LeadRead)
def get_lead_route(lead_id: int, db: Session = Depends(get_db)) -> LeadRead:
    return get_lead(db, lead_id)
