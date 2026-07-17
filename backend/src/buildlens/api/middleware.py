"""Cross-cutting request middleware: request-id assignment and logging context.

Every request gets a unique id (or reuses the client-supplied X-Request-ID),
bound into the structlog context so all events during the request carry it,
and echoed back in the response headers (NFR-13).
"""

import uuid

import structlog
from fastapi import FastAPI, Request


REQUEST_ID_HEADER = "X-Request-ID"
_MAX_INBOUND_ID_LENGTH = 128


def add_request_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = inbound[:_MAX_INBOUND_ID_LENGTH] or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response