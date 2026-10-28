"""HTTP 请求访问日志中间件。"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

access_logger = logging.getLogger("access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录 method/path/status/耗时；跳过静态资源刷屏。"""

    SKIP_PREFIXES = ("/static/", "/favicon.ico")

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
        request.state.request_id = req_id
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = req_id
            return response
        except Exception:
            access_logger.exception(
                "req=%s %s %s -> EXCEPTION",
                req_id,
                request.method,
                path,
            )
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            access_logger.info(
                "req=%s %s %s -> %s %.1fms",
                req_id,
                request.method,
                path,
                status,
                elapsed_ms,
            )
