import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.commercial.models import Lead
from app.commercial.schemas import LeadCreate
from app.context import default_context
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import LEADS_CREATE, LEADS_READ
from app.iam.service import require_permission
from app.tenancy import resolve_organization_id, scoped

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


def _validate_service_need(
    session: Session, service_need_id: int | None, organization_id: int
) -> None:
    """A lead may only need a service from its own organization's catalog.

    ``fk_leads_organization_service_need`` enforces it in PostgreSQL; this check
    exists to produce the stable ``NOT_FOUND`` / ``ENTITY_INACTIVE`` contract.
    """
    if service_need_id is None:
        return
    service = session.scalar(
        scoped(
            select(Service).where(Service.id == service_need_id), Service, organization_id
        )
    )
    if service is None:
        raise AppError(ErrorCode.NOT_FOUND, "Service not found.")
    if not service.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Service is inactive.")


def create_lead(
    session: Session,
    data: LeadCreate,
    organization_id: int | None = None,
    ctx: ExecutionContext | None = None,
) -> Lead:
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        # BLOCKER-2 resolution: a Lead has no location dimension, so this is
        # an organization-wide operation — only an org-wide grant satisfies it
        # (E5); a location-scoped grant can never create leads.
        require_permission(session, resolved, LEADS_CREATE, location_id=None)

    phone = _normalize_phone(data.contact_phone)
    email = _normalize_contact_email(data.contact_email)
    source = _validate_acquisition_source(data.acquisition_source)

    if phone is None and email is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "At least one of contact_phone or contact_email is required.",
        )

    _validate_service_need(session, data.service_need_id, org_id)

    lead = Lead(
        organization_id=org_id,
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


def get_lead(session: Session, lead_id: int, organization_id: int | None = None) -> Lead:
    org_id = resolve_organization_id(organization_id)
    lead = session.scalar(scoped(select(Lead).where(Lead.id == lead_id), Lead, org_id))
    if lead is None:
        raise AppError(ErrorCode.NOT_FOUND, "Lead not found.")
    return lead


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    """Explicit ctx wins; otherwise the trusted/default context (PF3 seam)."""
    return ctx if ctx is not None else default_context(organization_id)


def list_leads(
    session: Session,
    *,
    ctx: ExecutionContext | None = None,
    organization_id: int | None = None,
    search: str | None = None,
    commercial_status: str | None = None,
) -> list[Lead]:
    """Lead list for the CRM/booking selector, tenant-scoped.

    ``search`` matches name or phone with a case-insensitive substring; the
    tenant comes from the context, never from the request body (X3). The
    permission check is org-wide (there is no lead location scope).
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, LEADS_READ)

    statement = scoped(select(Lead), Lead, org_id).order_by(Lead.full_name)
    if search:
        like = f"%{search}%"
        statement = statement.where(
            or_(Lead.full_name.ilike(like), Lead.contact_phone.ilike(like))
        )
    if commercial_status is not None:
        statement = statement.where(Lead.commercial_status == commercial_status)
    return list(session.scalars(statement))
