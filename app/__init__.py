from fastapi import Depends, FastAPI

from app.catalog.router import router as catalog_router
from app.context import require_authenticated_context
from app.clinical.router import router as clinical_router
from app.commercial.router import router as commercial_router
from app.economics.router import router as economics_router
from app.errors import register_error_handlers
from app.inventory.router import router as inventory_router
from app.organization.router import router as organization_router
from app.scheduling.router import router as scheduling_router


def create_app() -> FastAPI:
    app = FastAPI(title="OdontoFlow", version="0.1.0")
    register_error_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # One gate for every business route. ``/health`` stays open on purpose:
    # monitoring must not need a credential. Adding a router without this
    # dependency is the failure mode ``tests/test_authentication.py`` guards.
    authenticated = [Depends(require_authenticated_context)]
    for business_router in (
        catalog_router,
        clinical_router,
        commercial_router,
        economics_router,
        inventory_router,
        organization_router,
        scheduling_router,
    ):
        app.include_router(business_router, dependencies=authenticated)

    return app


app = create_app()
