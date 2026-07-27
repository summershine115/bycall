"""可观测性（M5）单测。

聚焦不涉及全局 OTel provider 的不变量——provider 注册（setup_tracing）的端到端
验证留给冒烟测试（enabled 时手动跑看 console span），避免全局 trace 状态污染测试套件。

覆盖：
- correlation_id 生命周期（contextvars set/get/reset）
- 结构化日志（CorrelationFilter 注入 cid + Json/Readable formatter）
- setup_observability(enabled=False) 完全 no-op（不改 root logger）
- traced 装饰器在 noop tracer（未 setup）下正常执行
- ASGI middleware（纯 ASGI，流式安全）：生成 cid + 回写响应头 + 接受入站头 + 请求内可见
"""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from bycall.observability import (
    AccessLogMiddleware,
    CorrelationMiddleware,
    get_correlation_id,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
    setup_observability,
    traced,
)
from bycall.observability.logging_setup import (
    CorrelationFilter,
    JsonFormatter,
    ReadableFormatter,
    setup_logging,
)

# ============ correlation_id ============


def test_new_correlation_id_is_16_hex():
    cid = new_correlation_id()
    assert len(cid) == 16
    int(cid, 16)  # 合法 hex
    # 每次生成不同
    assert new_correlation_id() != cid


def test_correlation_id_set_get_reset():
    assert get_correlation_id() is None  # 默认无
    token = set_correlation_id("abc123")
    assert get_correlation_id() == "abc123"
    reset_correlation_id(token)
    assert get_correlation_id() is None


# ============ 结构化日志 ============


def _make_record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None
    )


def test_correlation_filter_injects_id():
    token = set_correlation_id("cid-xyz")
    try:
        record = _make_record()
        assert CorrelationFilter().filter(record)
        assert record.correlation_id == "cid-xyz"  # type: ignore[attr-defined]
    finally:
        reset_correlation_id(token)


def test_correlation_filter_defaults_to_dash_when_unset():
    record = _make_record()
    CorrelationFilter().filter(record)
    assert record.correlation_id == "-"  # type: ignore[attr-defined]


def test_json_formatter_contains_correlation_id():
    token = set_correlation_id("cid-1")
    try:
        import json

        record = _make_record("支付完成")
        CorrelationFilter().filter(record)
        line = JsonFormatter().format(record)
        payload = json.loads(line)
        assert payload["correlation_id"] == "cid-1"
        assert payload["msg"] == "支付完成"
        assert payload["level"] == "INFO"
    finally:
        reset_correlation_id(token)


def test_readable_formatter_has_cid_prefix():
    token = set_correlation_id("cid-2")
    try:
        record = _make_record()
        CorrelationFilter().filter(record)
        line = ReadableFormatter().format(record)
        assert line.startswith("[cid-2]")
    finally:
        reset_correlation_id(token)


def test_setup_logging_mounts_single_handler():
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        setup_logging(structured=True, level="DEBUG")
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
        # 幂等：再调一次仍只一个 handler
        setup_logging(structured=False, level="INFO")
        assert len(root.handlers) == 1
    finally:
        # 恢复 root，避免污染其他用例
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


# ============ setup_observability disabled ============


def test_setup_observability_disabled_is_noop():
    """enabled=False 时不改 root logger（默认关闭，现状零影响）。"""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    setup_observability(
        enabled=False,
        service_name="x",
        otlp_endpoint="",
        sampler_ratio=1.0,
        structured_logs=False,
    )
    assert list(root.handlers) == handlers_before


# ============ traced 装饰器（noop tracer） ============


def test_traced_decorator_runs_when_tracing_not_setup():
    """未 setup tracing 时是 noop tracer，traced 仍正常执行并返回值。"""

    @traced("test.add")
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_traced_decorator_propagates_exception():
    """被装饰函数抛异常时，traced 不吞错（span 由装饰器 record，此处只验不吞）。"""

    @traced("test.boom")
    def boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        boom()


# ============ ASGI middleware ============


def _make_hello_app(seen: dict | None = None) -> Starlette:
    async def hello(request):
        if seen is not None:
            # 验证 correlation_id 在请求处理期间可见（contextvars 跨 middleware→handler 传播）。
            seen["cid"] = get_correlation_id()
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", hello)])


def test_correlation_middleware_generates_id_and_writes_header():
    app = _make_hello_app()
    app.add_middleware(CorrelationMiddleware)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    cid = resp.headers["x-request-id"]
    assert len(cid) == 16
    int(cid, 16)  # 合法 hex


def test_correlation_middleware_accepts_inbound_header():
    """上游可传 X-Request-ID 做跨服务关联，middleware 原样回写。"""
    app = _make_hello_app()
    app.add_middleware(CorrelationMiddleware)
    client = TestClient(app)
    resp = client.get("/", headers={"X-Request-ID": "upstream-trace-1"})
    assert resp.headers["x-request-id"] == "upstream-trace-1"


def test_correlation_id_visible_inside_request_handler():
    """correlation_id 写入 contextvars 后，请求 handler 内能取到（贯穿日志/trace 的基础）。"""
    seen: dict = {}
    app = _make_hello_app(seen)
    app.add_middleware(CorrelationMiddleware)
    client = TestClient(app)
    client.get("/", headers={"X-Request-ID": "handler-visible"})
    assert seen["cid"] == "handler-visible"


def test_access_log_middleware_logs_request(caplog):
    app = _make_hello_app()
    app.add_middleware(AccessLogMiddleware)
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="bycall.access"):
        resp = client.get("/")
    assert resp.status_code == 200
    assert any(
        r.name == "bycall.access" and "request GET / 200" in r.message for r in caplog.records
    )
