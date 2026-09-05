import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG


class _PingBody(BaseModel):
    name: str


@pytest.fixture
def error_app(migrated_engine):
    app = create_app()

    test_engine = create_engine(get_settings().test_database_url, pool_pre_ping=True)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def db_session():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    @app.post("/test/validation")
    def _validation(payload: _PingBody):
        return {"ok": payload.name}

    @app.post("/test/not-found")
    def _not_found():
        raise AppError(ErrorCode.NOT_FOUND, "The requested resource was not found.")

    @app.post("/test/inactive")
    def _inactive():
        raise AppError(ErrorCode.ENTITY_INACTIVE, "The referenced entity is inactive.")

    @app.post("/test/conflict")
    def _conflict():
        raise AppError(
            ErrorCode.APPOINTMENT_CONFLICT,
            "The requested appointment slot is no longer available.",
        )

    @app.post("/test/overlap")
    def _overlap(session=Depends(db_session)):
        with session.begin():
            session.execute(text(f"INSERT INTO services (organization_id, name, duration_minutes) VALUES ({ORG}, 'Limpieza', 30)"))
            session.execute(text(f"INSERT INTO locations (organization_id, name, timezone) VALUES ({ORG}, 'Sede Centro', 'America/Lima')"))
            session.execute(text("INSERT INTO practitioners (display_name) VALUES ('Dra. Ana')"))
            session.execute(text(f"INSERT INTO practitioner_memberships (organization_id, practitioner_id) VALUES ({ORG}, 1)"))
            session.execute(text(f"INSERT INTO leads (organization_id, full_name, contact_phone, acquisition_source) VALUES ({ORG}, 'Juan', '+51999000111', 'direct')"))
        session.execute(
            text(
                "INSERT INTO appointments (organization_id, lead_id, service_id, practitioner_id, location_id, start_utc, end_utc, state) "
                f"VALUES ({ORG}, 1, 1, 1, 1, '2026-08-13T09:00:00+00', '2026-08-13T10:00:00+00', 'confirmed')"
            )
        )
        session.commit()
        session.execute(
            text(
                "INSERT INTO appointments (organization_id, lead_id, service_id, practitioner_id, location_id, start_utc, end_utc, state) "
                f"VALUES ({ORG}, 1, 1, 1, 1, '2026-08-13T09:30:00+00', '2026-08-13T10:30:00+00', 'confirmed')"
            )
        )
        session.commit()

    @app.post("/test/duplicate")
    def _duplicate(session=Depends(db_session)):
        with session.begin():
            session.execute(text(f"INSERT INTO services (organization_id, name, duration_minutes) VALUES ({ORG}, 'Limpieza', 30)"))
        session.execute(text(f"INSERT INTO services (organization_id, name, duration_minutes) VALUES ({ORG}, 'Limpieza', 30)"))
        session.commit()

    return app


@pytest.fixture
def client(error_app):
    return TestClient(error_app, raise_server_exceptions=False, headers=AUTH_HEADERS)


def test_app_error_not_found_returns_404_envelope(client):
    response = client.post("/test/not-found")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "The requested resource was not found."
    assert body["error"]["details"] == {}


def test_app_error_entity_inactive_returns_409_envelope(client):
    response = client.post("/test/inactive")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "ENTITY_INACTIVE"
    assert body["error"]["details"] == {}


def test_app_error_conflict_returns_409_envelope(client):
    response = client.post("/test/conflict")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "APPOINTMENT_CONFLICT"
    assert body["error"]["message"] == "The requested appointment slot is no longer available."


def test_validation_error_returns_422_invalid_input(client):
    response = client.post("/test/validation", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert "detail" not in body
    assert "loc" not in body["error"]
    assert "Traceback" not in response.text


def test_exclusion_violation_returns_409_appointment_conflict(client):
    response = client.post("/test/overlap")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "APPOINTMENT_CONFLICT"
    assert "conflicting key" not in response.text
    assert "excl_appointments_confirmed_no_overlap" not in response.text
    assert "23P01" not in response.text


def test_unknown_integrity_error_is_not_appointment_conflict(client):
    response = client.post("/test/duplicate")
    assert response.status_code == 500
    assert "APPOINTMENT_CONFLICT" not in response.text
    assert "Limpieza" not in response.text
    assert "Traceback" not in response.text


def test_health_unchanged():
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
