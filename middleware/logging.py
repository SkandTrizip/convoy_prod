import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from logging_config import get_access_logger

access_logger = get_access_logger()


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        client = request.client.host if request.client else "-"
        path = request.url.path
        query = str(request.url.query)
        full_path = f"{path}?{query}" if query else path

        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000
            access_logger.info(
                "%s %s | status=%s | %.1fms | client=%s",
                request.method,
                full_path,
                response.status_code,
                duration_ms,
                client,
            )
            return response
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            access_logger.exception(
                "%s %s | status=500 | %.1fms | client=%s",
                request.method,
                full_path,
                duration_ms,
                client,
            )
            raise
