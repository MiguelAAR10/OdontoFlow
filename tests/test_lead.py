import pytest
from sqlalchemy import func, select, text

from app.catalog.models import Service
from app.catalog.schemas import ServiceCreate
from app.catalog.service import create_service
from app.commercial.models import Lead
from app.commercial.schemas import LeadCreate
from app.commercial.service import create_lead, get_lead
from app.errors import AppError, ErrorCode


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_valid_direct_lead_with_phone_persists(session):
    lead = create_lead(
        session,
        LeadCreate(
            full_name="Juan Pérez",
            contact_phone="+51 999 000 111",
            acquisition_source="direct",
        ),
    )
    assert lead.id is not None
    assert lead.commercial_status == "new"
    assert session.get(Lead, lead.id) is not None
    fetched = get_lead(session, lead.id)
    assert fetched.full_name == "Juan Pérez"
    assert fetched.acquisition_source == "direct"
    assert _count(session, Lead) == 1


def test_valid_referral_lead_with_email_persists(session):
    lead = create_lead(
        session,
        LeadCreate(
            full_name="María Gómez",
            contact_email="maria@example.com",
            acquisition_source="referral",
        ),
    )
    assert lead.id is not None
    assert lead.contact_email == "maria@example.com"
    assert lead.contact_phone is None
    fetched = get_lead(session, lead.id)
    assert fetched.acquisition_source == "referral"
    assert _count(session, Lead) == 1


def test_valid_promotion_lead_with_service_need_persists(session):
    service = create_service(session, ServiceCreate(name="Limpieza", duration_minutes=30))
    lead = create_lead(
        session,
        LeadCreate(
            full_name="Carlos Ruiz",
            contact_phone="+51 987 654 321",
            acquisition_source="promotion",
            service_need_id=service.id,
        ),
    )
    assert lead.id is not None
    assert lead.service_need_id == service.id
    assert lead.commercial_status == "new"
    fetched = get_lead(session, lead.id)
    assert fetched.service_need_id == service.id
    assert _count(session, Lead) == 1


def test_neither_phone_nor_email_rejected(session):
    with pytest.raises(AppError) as exc_info:
        create_lead(
            session,
            LeadCreate(full_name="Ana Torres", acquisition_source="direct"),
        )
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert _count(session, Lead) == 0


def test_unsupported_acquisition_source_rejected(session):
    data = LeadCreate.model_construct(
        full_name="Pedro Díaz",
        contact_phone="+51 111",
        acquisition_source="walkin",
    )
    with pytest.raises(AppError) as exc_info:
        create_lead(session, data)
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert _count(session, Lead) == 0


def test_phone_normalization_deterministic(session):
    lead = create_lead(
        session,
        LeadCreate(
            full_name="Lucía Fernández",
            contact_phone="+51 999-001-111",
            acquisition_source="direct",
        ),
    )
    assert lead.contact_phone == "+51999001111"
    fetched = get_lead(session, lead.id)
    assert fetched.contact_phone == "+51999001111"


def test_missing_service_need_raises_not_found(session):
    with pytest.raises(AppError) as exc_info:
        create_lead(
            session,
            LeadCreate(
                full_name="Sofía Castro",
                contact_phone="+51 222 333 444",
                acquisition_source="referral",
                service_need_id=999999,
            ),
        )
    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert _count(session, Lead) == 0


def test_inactive_service_need_raises_entity_inactive(session):
    service = create_service(
        session, ServiceCreate(name="Blanqueamiento", duration_minutes=60)
    )
    service.is_active = False
    session.commit()
    with pytest.raises(AppError) as exc_info:
        create_lead(
            session,
            LeadCreate(
                full_name="Diego Ramos",
                contact_phone="+51 555 666 777",
                acquisition_source="promotion",
                service_need_id=service.id,
            ),
        )
    assert exc_info.value.code == ErrorCode.ENTITY_INACTIVE
    assert _count(session, Lead) == 0


def test_no_service_need_persists_with_null(session):
    lead = create_lead(
        session,
        LeadCreate(
            full_name="Valeria Ortiz",
            contact_email="valeria@example.com",
            acquisition_source="direct",
        ),
    )
    assert lead.service_need_id is None
    fetched = get_lead(session, lead.id)
    assert fetched.service_need_id is None
    assert _count(session, Lead) == 1


def test_lead_distinct_from_patient(session):
    create_lead(
        session,
        LeadCreate(
            full_name="Paciente de Prueba",
            contact_phone="+51 888 999 000",
            acquisition_source="direct",
        ),
    )
    tables = {
        row[0]
        for row in session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    assert "leads" in tables
    # Lead and Patient are distinct entities: the legacy "pacientes" table
    # never appears, and the PF5 clinical table is a separate, org-owned one.
    assert "patients" in tables
    assert not {"pacientes", "patient", "lead_patients"} & tables
