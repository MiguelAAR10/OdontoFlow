from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent_tools.router import router as agent_tools_router
from app.catalog.router import router as catalog_router
from app.config import get_settings
from app.context import require_authenticated_context
from app.clinical.router import router as clinical_router
from app.commercial.router import router as commercial_router
from app.economics.router import router as economics_router
from app.errors import register_error_handlers
from app.inventory.router import router as inventory_router
from app.http_security import SecurityBoundaryMiddleware, install_security_openapi
from app.messaging.router import router as messaging_router
from app.organization.router import router as organization_router
from app.scheduling.router import router as scheduling_router


def create_app() -> FastAPI:
    settings = get_settings()
    docs_enabled = settings.app_env == "development"
    app = FastAPI(
        title="OdontoFlow",
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.security_settings = settings
    register_error_handlers(app)

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Request-Id",
                "X-Correlation-Id",
            ],
        )
    app.add_middleware(SecurityBoundaryMiddleware, settings=settings)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # One gate for every business route. ``/health`` stays open on purpose:
    # monitoring must not need a credential. Adding a router without this
    # dependency is the failure mode ``tests/test_authentication.py`` guards.
    authenticated = [Depends(require_authenticated_context)]
    for business_router in (
        agent_tools_router,
        catalog_router,
        clinical_router,
        commercial_router,
        economics_router,
        inventory_router,
        messaging_router,
        organization_router,
        scheduling_router,
    ):
        app.include_router(business_router, dependencies=authenticated)

    install_security_openapi(app)

    return app


app = create_app()
