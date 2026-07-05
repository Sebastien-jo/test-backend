"""HTTP middleware: request correlation and access logging.

Generates (or adopts) a `request_id` per request, binds it to the structlog
context so every log line emitted while handling the request carries it, and
echoes it back as `X-Request-ID`. When the request enqueues Celery tasks, the
same id rides along into the worker (see `app.workers.celery_app`), so one id
follows an upload from HTTP to the last task of the DAG.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger("api.request")

_REQUEST_ID_HEADER = "X-Request-ID"
# Health checks are high-frequency and uninteresting; SSE requests are
# long-lived streams logged at open/close by the endpoint itself.
_SKIP_LOG_PATHS = frozenset({"/health"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        # Fresh context per request, then bind the id for every downstream log.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0

        response.headers[_REQUEST_ID_HEADER] = request_id

        path = request.url.path
        if path not in _SKIP_LOG_PATHS and not path.endswith("/events"):
            logger.info(
                "request",
                method=request.method,
                path=path,
                http_status=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        return response
