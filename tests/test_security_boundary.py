"""Permanent negative proofs for the HTTP security boundary.

These tests describe the contract an external integration can rely on before
OdontoFlow is exposed to n8n or a public webhook.  They intentionally exercise
the real FastAPI application and PostgreSQL: authentication, authorization,
rate limiting and audit provenance are one boundary, not isolated helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from conftest import AUTH_HEADERS
from app import create_app
from app.config import get_settings
from app.context import require_authenticated_context
from app.db import get_db
from app.iam.credentials import issue_credential
from app.iam.models import Membership, Principal, Role, RoleAssignment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID

ORG = BOOTSTRAP_ORGANIZATION_ID
WINDOW_START = "2026-08-10T00:00:00Z"
WINDOW_END = "2026-08-11T00:00:00Z"


def _app_for(migrated_engine):
    app = create_app()
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return app


def _token_for_member_without_roles(session, *, name: str = "no-role") -> str:
    principal = Principal(type="integration", display_name=name)
    session.add(principal)
    session.flush()
    session.add(Membership(organization_id=ORG, principal_id=principal.id))
    session.flush()
    _credential, token = issue_credential(
        session,
        organization_id=ORG,
        principal_id=principal.id,
        name=name,
    )
    session.commit()
    return token


def _token_with_all_permissions(session, *, name: str) -> str:
    principal = Principal(type="integration", display_name=name)
    session.add(principal)
    session.flush()
    membership = Membership(organization_id=ORG, principal_id=principal.id)
    role = Role(organization_id=ORG, code=name, name=name)
    session.add_all((membership, role))
    session.flush()
    session.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT :role, id FROM permissions"
        ),
        {"role": role.id},
    )
    session.add(
        RoleAssignment(
            organization_id=ORG,
            membership_id=membership.id,
            role_id=role.id,
        )
    )
    session.flush()
    _credential, token = issue_credential(
        session,
        organization_id=ORG,
        principal_id=principal.id,
        name=name,
    )
    session.commit()
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _outbound_claim(
    client, *, token: str | None = None, extra_headers: dict[str, str] | None = None
):
    headers = {"Idempotency-Key": str(uuid4())}
    if token is not None:
        headers.update(_auth(token))
    if extra_headers is not None:
        headers.update(extra_headers)
    return client.post(
        "/internal/outbound/claim",
        json={"limit": 1},
        headers=headers,
    )


@pytest.mark.parametrize("path", ("/internal/outbound/claim", "/agent-tools/call"))
def test_integration_routes_deny_a_member_without_permissions(
    migrated_engine, session, path
):
    token = _token_for_member_without_roles(session)
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        if path == "/internal/outbound/claim":
            response = _outbound_claim(client, token=token)
        else:
            request_id = str(uuid4())
            response = client.post(
                path,
                json={
                    "tool_version": "1.0",
                    "tool_name": "get_reception_context",
                    "conversation_id": 999999,
                    "request_id": request_id,
                    "correlation_id": request_id,
                    "idempotency_key": None,
                    "arguments": {},
                },
                headers={**_auth(token), "X-Request-Id": request_id, "X-Correlation-Id": request_id},
            )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_only_integration_routes_have_the_authentication_dependency():
    app = create_app()
    public_paths = {"/health"}
    documentation_paths = {app.docs_url, app.redoc_url, app.openapi_url}

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in public_paths | documentation_paths:
            continue
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        is_integration = route.path.startswith(("/internal/", "/agent-tools/"))
        assert (require_authenticated_context in dependencies) is is_integration, route.path


def test_routers_cannot_reintroduce_the_system_default_identity():
    from pathlib import Path

    router_files = Path("app").glob("*/router.py")
    forbidden = ("default_context", "SYSTEM_PRINCIPAL_ID")
    for router_file in router_files:
        source = router_file.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, f"{router_file} imports {name}"


def test_openapi_declares_bearer_authentication_on_business_operations():
    schema = create_app().openapi()
    scheme = schema["components"]["securitySchemes"]["IntegrationBearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"

    assert "security" not in schema["paths"]["/health"]["get"]
    for path, path_item in schema["paths"].items():
        if path == "/health":
            continue
        is_integration = path.startswith(("/internal/", "/agent-tools/"))
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if is_integration:
                assert {"IntegrationBearer": []} in operation.get("security", []), (
                    method,
                    path,
                )
            else:
                assert "security" not in operation, (method, path)
            parameters = {
                (parameter["in"], parameter["name"]): parameter
                for parameter in operation.get("parameters", [])
            }
            assert parameters[("header", "X-Request-Id")]["schema"]["format"] == "uuid"
            assert parameters[("header", "X-Correlation-Id")]["schema"]["format"] == "uuid"
            for status in ("401", "403", "429", "503"):
                assert status in operation["responses"], (method, path, status)

            for response in operation["responses"].values():
                assert "X-Request-Id" in response.get("headers", {})
                assert "X-Correlation-Id" in response.get("headers", {})


def test_failed_authentication_is_a_real_redacted_security_event(migrated_engine, session):
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    secret = "supersecretvalue123456"
    headers = {
        **_auth(f"ofk_deadbeef_{secret}"),
        "X-Request-Id": request_id,
        "X-Correlation-Id": correlation_id,
    }
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = _outbound_claim(client, extra_headers=headers)

    assert response.status_code == 401
    event = session.execute(
        text(
            "SELECT event_type, outcome, organization_id, principal_id, "
            "request_id, correlation_id, metadata FROM security_events "
            "ORDER BY id DESC LIMIT 1"
        )
    ).mappings().one()
    assert event["event_type"] == "authentication"
    assert event["outcome"] == "failed"
    assert event["organization_id"] is None
    assert event["principal_id"] is None
    assert event["request_id"] == request_id
    assert event["correlation_id"] == correlation_id
    assert secret not in str(dict(event))


def test_trace_ids_are_validated_returned_and_persisted(migrated_engine, session):
    request_id = str(uuid4())
    correlation_id = str(uuid4())
    headers = {
        **AUTH_HEADERS,
        "X-Request-Id": request_id,
        "X-Correlation-Id": correlation_id,
    }
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.post("/patients", json={"full_name": "Paciente Traza"}, headers=headers)

    assert response.status_code == 201, response.text
    assert response.headers["X-Request-Id"] == request_id
    assert response.headers["X-Correlation-Id"] == correlation_id
    UUID(response.headers["X-Request-Id"])
    UUID(response.headers["X-Correlation-Id"])

    audit = session.execute(
        text(
            "SELECT request_id, correlation_id FROM audit_events "
            "WHERE action = 'patient.created' ORDER BY id DESC LIMIT 1"
        )
    ).mappings().one()
    assert audit == {"request_id": request_id, "correlation_id": correlation_id}


@pytest.mark.parametrize("header", ("X-Request-Id", "X-Correlation-Id"))
def test_invalid_trace_ids_are_rejected_before_the_domain(migrated_engine, header):
    headers = {**AUTH_HEADERS, header: "not-a-uuid"}
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.get("/services", headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"


def test_security_headers_are_present_even_on_authentication_errors(migrated_engine):
    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="https://testserver",
    ) as client:
        response = client.post(
            "/internal/outbound/claim",
            json={"limit": 1},
            headers={"Idempotency-Key": str(uuid4())},
        )

    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert response.headers["Cache-Control"] == "no-store"


def test_oversized_json_is_rejected_before_validation(migrated_engine):
    payload = {"name": "x" * 300_000, "duration_minutes": 30}
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.post("/services", json=payload, headers=AUTH_HEADERS)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_production_disables_interactive_docs_and_requires_https(monkeypatch, migrated_engine):
    monkeypatch.setenv("APP_ENV", "production")
    app = _app_for(migrated_engine)

    with TestClient(app, raise_server_exceptions=False, base_url="https://testserver") as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404

    with TestClient(app, raise_server_exceptions=False, base_url="http://testserver") as client:
        response = client.get("/health")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "HTTPS_REQUIRED"

    with TestClient(app, raise_server_exceptions=False, base_url="https://testserver") as client:
        response = client.get("/services")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_erp_anonymous_compat_defaults_on_in_development(monkeypatch, migrated_engine):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("ERP_ANONYMOUS_COMPAT", raising=False)
    assert get_settings().erp_anonymous_compat is True
    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="https://testserver",
    ) as client:
        response = client.get("/services")
    assert response.status_code == 200, response.text


def test_erp_anonymous_compat_defaults_off_in_production(monkeypatch, migrated_engine):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ERP_ANONYMOUS_COMPAT", raising=False)
    assert get_settings().erp_anonymous_compat is False
    with TestClient(
        _app_for(migrated_engine),
        raise_server_exceptions=False,
        base_url="https://testserver",
    ) as client:
        response = client.get("/services")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_erp_anonymous_compat_cannot_be_enabled_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ERP_ANONYMOUS_COMPAT", "true")
    with pytest.raises(ValueError, match="must be false in production"):
        get_settings()


def test_integration_kill_switch_fails_closed(monkeypatch, migrated_engine):
    monkeypatch.setenv("INTEGRATION_API_ENABLED", "false")
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = _outbound_claim(client, token="ofk_testtest_integration-secret-for-tests")
        health = client.get("/health")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INTEGRATION_DISABLED"
    assert health.status_code == 200


def test_rate_limit_is_shared_and_scoped_per_credential(
    monkeypatch, migrated_engine, session
):
    monkeypatch.setenv("RATE_LIMIT_MUTATIONS_PER_MINUTE", "2")
    token_a = _token_with_all_permissions(session, name="rate-a")
    token_b = _token_with_all_permissions(session, name="rate-b")

    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        assert _outbound_claim(client, token=token_a).status_code == 200
        assert _outbound_claim(client, token=token_a).status_code == 200
        limited = _outbound_claim(client, token=token_a)
        independent = _outbound_claim(client, token=token_b)

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert independent.status_code == 200


def test_cors_is_closed_by_default(migrated_engine):
    headers = {
        "Origin": "https://attacker.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    }
    with TestClient(_app_for(migrated_engine), raise_server_exceptions=False) as client:
        response = client.options("/services", headers=headers)

    assert "Access-Control-Allow-Origin" not in response.headers


def test_production_runner_refuses_untrusted_plain_http(monkeypatch):
    from app.run import main

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("TLS_CERTFILE", raising=False)
    monkeypatch.delenv("TLS_KEYFILE", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)

    with pytest.raises(RuntimeError, match="requires local TLS"):
        main()


def test_runner_disables_server_banner_and_trusts_only_explicit_proxies(monkeypatch):
    import app.run as runner

    captured = {}
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.10,10.0.0.11")
    monkeypatch.setattr(runner.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs))

    runner.main()

    assert captured["server_header"] is False
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "10.0.0.10,10.0.0.11"


def test_credential_profile_assigns_only_its_minimum_permission(session):
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=ORG,
        name="profile-inbound",
        principal_type="integration",
    )
    _assign_profile(
        session,
        organization_id=ORG,
        principal_id=principal.id,
        profile="n8n-inbound",
    )

    codes = {
        row[0]
        for row in session.execute(
            text(
                "SELECT p.code FROM permissions p "
                "JOIN role_permissions rp ON rp.permission_id=p.id "
                "JOIN roles r ON r.id=rp.role_id "
                "JOIN role_assignments ra ON ra.role_id=r.id "
                "JOIN memberships m ON m.id=ra.membership_id "
                "WHERE m.principal_id=:principal"
            ),
            {"principal": principal.id},
        )
    }
    assert codes == {"messages.create"}


def test_conversation_agent_profile_is_contact_safe_and_booking_bounded(session):
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=ORG,
        name="profile-conversation-agent",
        principal_type="agent",
    )
    _assign_profile(
        session,
        organization_id=ORG,
        principal_id=principal.id,
        profile="conversation-agent",
    )

    codes = {
        row[0]
        for row in session.execute(
            text(
                "SELECT p.code FROM permissions p "
                "JOIN role_permissions rp ON rp.permission_id=p.id "
                "JOIN roles r ON r.id=rp.role_id "
                "JOIN role_assignments ra ON ra.role_id=r.id "
                "JOIN memberships m ON m.id=ra.membership_id "
                "WHERE m.principal_id=:principal"
            ),
            {"principal": principal.id},
        )
    }
    assert codes == {
        "availability.read",
        "contact_appointments.book",
        "contact_appointments.cancel",
        "contact_appointments.read",
        "contact_appointments.reschedule",
        "contact_profiles.manage",
        "conversations.manage",
        "conversations.read",
        "deliveries.create",
        "locations.read",
        "practitioners.read",
        "services.read",
    }


def test_sales_agent_v0_profile_is_least_privilege(session):
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=ORG,
        name="profile-sales-agent-v0",
        principal_type="agent",
    )
    _assign_profile(
        session,
        organization_id=ORG,
        principal_id=principal.id,
        profile="sales-agent-v0",
    )

    codes = {
        row[0]
        for row in session.execute(
            text(
                "SELECT p.code FROM permissions p "
                "JOIN role_permissions rp ON rp.permission_id=p.id "
                "JOIN roles r ON r.id=rp.role_id "
                "JOIN role_assignments ra ON ra.role_id=r.id "
                "JOIN memberships m ON m.id=ra.membership_id "
                "WHERE m.principal_id=:principal"
            ),
            {"principal": principal.id},
        )
    }
    assert codes == {
        "conversations.read",
        "services.read",
        "locations.read",
        "availability.read",
        "contact_appointments.read",
        "contact_appointments.book",
        "conversations.manage",
        "deliveries.create",
    }


def test_reception_operator_profile_can_resume_without_agent_business_tools(session):
    from scripts.issue_credential import _assign_profile, _resolve_principal

    principal = _resolve_principal(
        session,
        organization_id=ORG,
        name="profile-reception-operator",
        principal_type="integration",
    )
    _assign_profile(
        session,
        organization_id=ORG,
        principal_id=principal.id,
        profile="reception-operator",
    )

    codes = {
        row[0]
        for row in session.execute(
            text(
                "SELECT p.code FROM permissions p "
                "JOIN role_permissions rp ON rp.permission_id=p.id "
                "JOIN roles r ON r.id=rp.role_id "
                "JOIN role_assignments ra ON ra.role_id=r.id "
                "JOIN memberships m ON m.id=ra.membership_id "
                "WHERE m.principal_id=:principal"
            ),
            {"principal": principal.id},
        )
    }
    assert codes == {"conversations.read", "conversations.resume"}
