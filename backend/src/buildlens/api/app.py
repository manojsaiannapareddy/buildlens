"""FastAPI application factory and platform endpoints (liveness)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from buildlens.api.errors import add_error_handlers, problem_response
from buildlens.api.middleware import add_request_id_middleware
from buildlens.core.config import get_settings
from buildlens.core.logging_setup import configure_logging
from buildlens.db.session import create_engine, create_session_factory

READYZ_TIMEOUT_SECONDS = 3.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own application-scoped resources: build them on startup, release on shutdown."""
    logger = structlog.get_logger()

    engine = create_engine(get_settings())
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    logger.info("app.startup_complete")

    yield

    await engine.dispose()
    logger.info("app.shutdown_complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    logger = structlog.get_logger()
    logger.info("app.created", environment=settings.environment)

    app = FastAPI(title="buildlens", version="0.1.0", lifespan=lifespan)
    add_request_id_middleware(app)
    add_error_handlers(app)

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        logger.info("healthz.checked")
        return {"status": "ok", "environment": settings.environment}

    @app.get("/readyz", tags=["platform"])
    async def readyz() -> Response:
        try:
            async with asyncio.timeout(READYZ_TIMEOUT_SECONDS):
                async with app.state.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("readyz.failed", error=str(exc))
            return problem_response(
                status=503,
                title="Service Unavailable",
                detail="Database is not reachable.",
            )
        return JSONResponse({"status": "ready", "environment": settings.environment})

    return app
