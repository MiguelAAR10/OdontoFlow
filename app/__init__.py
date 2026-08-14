from fastapi import FastAPI

from app.catalog.router import router as catalog_router
from app.commercial.router import router as commercial_router
from app.errors import register_error_handlers
from app.organization.router import router as organization_router
from app.scheduling.router import router as scheduling_router


def create_app() -> FastAPI:
    app = FastAPI(title="OdontoFlow", version="0.1.0")
    register_error_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(catalog_router)
    app.include_router(commercial_router)
    app.include_router(organization_router)
    app.include_router(scheduling_router)

    return app


app = create_app()
