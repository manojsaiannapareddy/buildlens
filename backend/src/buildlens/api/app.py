"""FastAPI application factory and platform endpoints (liveness)."""

from fastapi import FastAPI

from buildlens.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="buildlens", version="0.1.0")

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    return app