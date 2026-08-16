from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.catalog.schemas import ServiceCreate
from app.context import default_context
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import SERVICES_MANAGE
from app.iam.service import require_permission
from app.tenancy import resolve_organization_id


def _resolved_context(
    ctx: ExecutionContext | None, organization_id: int | None
) -> ExecutionContext:
    return ctx if ctx is not None else default_context(organization_id)


def create_service(
    session: Session,
    data: ServiceCreate,
    organization_id: int | None = None,
    ctx: ExecutionContext | None = None,
) -> Service:
    """Create one service in the acting organization's catalog.

    The duplicate check is organization-scoped so two tenants may sell the same
    service name; ``uq_services_organization_name`` remains the final authority.
    """
    resolved = _resolved_context(ctx, organization_id)
    org_id = resolved.organization_id
    if ctx is not None:
        require_permission(session, resolved, SERVICES_MANAGE)
    existing = session.scalar(
        select(Service).where(
            Service.organization_id == org_id, Service.name == data.name
        )
    )
    if existing is not None:
        raise AppError(ErrorCode.INVALID_INPUT, "A service with this name already exists.")
    service = Service(
        organization_id=org_id,
        name=data.name,
        duration_minutes=data.duration_minutes,
        is_active=data.is_active,
    )
    session.add(service)
    session.commit()
    session.refresh(service)
    return service


def list_services(session: Session, organization_id: int | None = None) -> list[Service]:
    org_id = resolve_organization_id(organization_id)
    return list(
        session.scalars(
            select(Service)
            .where(Service.organization_id == org_id)
            .order_by(Service.name)
        )
    )
