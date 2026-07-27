"""ASGI middleware：生成/传播 correlation_id + HTTP 访问日志。

用纯 ASGI（非 starlette BaseHTTPMiddleware），避免对流式响应（/chat SSE、IM 流式）
的缓冲副作用——BaseHTTPMiddleware 会把响应体物化，破坏逐 token 推送。

职责：
- ``CorrelationMiddleware``：每个入站请求生成 cid（或接受 X-Request-ID 头），写入
  contextvars，并回写响应头 X-Request-ID（前端/排障可关联）。
- ``AccessLogMiddleware``：记每请求访问日志（method/path/status/耗时）。otel 启用与否都工作。
HTTP span 本身由 OTel FastAPIInstrumentor 自动生成，这里不重复记 span。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bycall.observability.correlation import (
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

logger = logging.getLogger("bycall.access")

_HEADER_REQUEST_ID = b"x-request-id"


class CorrelationMiddleware:
    """为每个 HTTP 请求建立 correlation_id 上下文，回写响应头。纯 ASGI，流式安全。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # 从请求头取 X-Request-ID（上游可传入做跨服务关联），无则生成。
        cid: str | None = None
        for key, value in scope.get("headers", []):
            if key == _HEADER_REQUEST_ID:
                cid = value.decode()
                break
        if not cid:
            cid = new_correlation_id()

        token = set_correlation_id(cid)

        async def send_with_cid(message: dict[str, Any]) -> None:
            # 在响应起始头注入 X-Request-ID，让客户端/排障能关联到本次请求。
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((_HEADER_REQUEST_ID, cid.encode()))  # type: ignore[arg-type]
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_cid)
        finally:
            reset_correlation_id(token)


class AccessLogMiddleware:
    """记每请求访问日志（method/path/status/耗时）。纯 ASGI，流式安全。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        start = time.perf_counter()
        status_holder = {"code": 0}

        async def send_logged(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.warning("request error %s %s %.1fms", method, path, elapsed_ms)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "request %s %s %d %.1fms", method, path, status_holder["code"], elapsed_ms
        )
