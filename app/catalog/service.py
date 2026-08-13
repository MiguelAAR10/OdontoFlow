from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Service
from app.catalog.schemas import ServiceCreate
from app.errors import AppError, ErrorCode


def create_service(session: Session, data: ServiceCreate) -> Service:
    existing = session.scalar(select(Service).where(Service.name == data.name))
    if existing is not None:
        raise AppError(ErrorCode.INVALID_INPUT, "A service with this name already exists.")
    service = Service(
        name=data.name,
        duration_minutes=data.duration_minutes,
        is_active=data.is_active,
    )
    session.add(service)
    session.commit()
    session.refresh(service)
    return service


def list_services(session: Session) -> list[Service]:
    return list(session.scalars(select(Service).order_by(Service.name)))
