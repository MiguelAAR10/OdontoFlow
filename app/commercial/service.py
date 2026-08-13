import re

from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.commercial.models import Lead
from app.commercial.schemas import LeadCreate
from app.errors import AppError, ErrorCode

ACQUISITION_SOURCES = ("promotion", "referral", "direct")


def _normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    phone = re.sub(r"[^\d+]", "", value)
    return phone if phone else None


def _normalize_contact_email(value: str | None) -> str | None:
    if value is None:
        return None
    email = value.strip()
    return email if email else None


def _validate_acquisition_source(source: str) -> str:
    if source not in ACQUISITION_SOURCES:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "acquisition_source must be one of 'promotion', 'referral', 'direct'.",
        )
    return source


def _validate_service_need(session: Session, service_need_id: int | None) -> None:
    if service_need_id is None:
        return
    service = session.get(Service, service_need_id)
    if service is None:
        raise AppError(ErrorCode.NOT_FOUND, "Service not found.")
    if not service.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Service is inactive.")


def create_lead(session: Session, data: LeadCreate) -> Lead:
    phone = _normalize_phone(data.contact_phone)
    email = _normalize_contact_email(data.contact_email)
    source = _validate_acquisition_source(data.acquisition_source)

    if phone is None and email is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "At least one of contact_phone or contact_email is required.",
        )

    _validate_service_need(session, data.service_need_id)

    lead = Lead(
        full_name=data.full_name,
        contact_phone=phone,
        contact_email=email,
        acquisition_source=source,
        service_need_id=data.service_need_id,
    )
    session.add(lead)
    session.commit()
    session.refresh(lead)
    return lead


def get_lead(session: Session, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise AppError(ErrorCode.NOT_FOUND, "Lead not found.")
    return lead
