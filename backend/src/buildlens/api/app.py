"""FastAPI application factory and platform endpoints (liveness)."""

import structlog
from fastapi import FastAPI

from buildlens.api.errors import add_error_handlers
from buildlens.api.middleware import add_request_id_middleware
from buildlens.core.config import get_settings
from buildlens.core.logging_setup import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    logger = structlog.get_logger()
    logger.info("app.created", environment=settings.environment)

    app = FastAPI(title="buildlens", version="0.1.0")
    add_request_id_middleware(app)
    add_error_handlers(app)

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        logger.info("healthz.checked")
        return {"status": "ok", "environment": settings.environment}

    return app