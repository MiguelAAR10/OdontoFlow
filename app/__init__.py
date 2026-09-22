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

    # The integration surface is the trust boundary for n8n and agents.
    # ``/health`` stays open on purpose: monitoring must not need a credential.
    #
    # CORE-02: the Lead-to-Appointment and Reception/Scheduling business
    # routers (commercial, catalog, organization, clinical, scheduling) are
    # gated here too. They used to rely solely on the per-endpoint
    # ``resolve_http_context`` call, which falls back to the seeded ``system``
    # identity whenever ``ERP_ANONYMOUS_COMPAT`` is enabled — an anonymous
    # caller with network access was a superuser over every one of those
    # routes. The router-level dependency authenticates first and caches the
    # resolved context on ``request.state`` (see ``require_authenticated_context``),
    # so ``resolve_http_context`` inside each handler — and the four
    # ``scheduling_router`` proposal routes that call it directly for their
    # human-only gate — simply reuses it instead of authenticating twice.
    #
    # Economics and inventory remain on the unmodified compatibility path:
    # they are unrelated legacy ERP surfaces this card does not close.
    authenticated = [Depends(require_authenticated_context)]
    authenticated_routers = (
        agent_tools_router,
        catalog_router,
        clinical_router,
        commercial_router,
        messaging_router,
        organization_router,
        scheduling_router,
    )
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
        dependencies = authenticated if business_router in authenticated_routers else []
        app.include_router(business_router, dependencies=dependencies)

    install_security_openapi(app)

    return app


app = create_app()
