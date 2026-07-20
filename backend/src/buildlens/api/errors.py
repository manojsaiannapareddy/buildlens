"""RFC 9457 problem-details error responses (spec §6.1).

Every error leaves the API in one shape, with request_id attached.
Expected errors are specific; unexpected errors are logged fully
server-side and returned as an opaque 500.
"""

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger()

PROBLEM_CONTENT_TYPE = "application/problem+json"


def problem_response(
    *, status: int, title: str, detail: str | None = None, type_slug: str = "about:blank"
) -> JSONResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    body = {
        "type": type_slug,
        "title": title,
        "status": status,
        "detail": detail,
        "request_id": request_id,
    }
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(
        status_code=status, 
        content=body, 
        media_type=PROBLEM_CONTENT_TYPE, 
        headers=headers
    )


def add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(status=exc.status_code, title=str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("request.unhandled_error", exc_info=exc)
        return problem_response(
            status=500,
            title="Internal Server Error",
            detail="An unexpected error occurred. Quote the request_id when reporting.",
        )