"""metrics（M5 Slice 2）单测。

验证：
- ``/metrics`` 端点暴露已注册指标（Prometheus 文本格式）
- ``MetricsMiddleware`` 记 HTTP RED（请求后对应 method/status_class label 出现）
- ``status_class`` 低基数映射（2xx/4xx/5xx）

注意：prometheus_client 全局 registry 是单例，指标跨测试累积——断言用「包含」而非「等于」。
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from bycall.observability.metrics import metrics_asgi_app, status_class
from bycall.observability.middleware import MetricsMiddleware


def test_status_class_is_low_cardinality():
    assert status_class(200) == "2xx"
    assert status_class(201) == "2xx"
    assert status_class(404) == "4xx"
    assert status_class(503) == "5xx"
    assert status_class(0) == "0xx"  # 未设置状态码兜底


def _hello_app() -> Starlette:
    async def hello(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", hello)])
    app.add_middleware(MetricsMiddleware)
    app.mount("/metrics", metrics_asgi_app())
    return app


def test_metrics_endpoint_exposes_registered_metrics():
    client = TestClient(_hello_app())
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    # 已注册的指标都在暴露文本里
    assert "bycall_http_requests_total" in text
    assert "bycall_http_request_duration_seconds" in text
    assert "bycall_rag_retrieval_duration_seconds" in text
    assert "bycall_rag_retrieved_chunks" in text


def test_metrics_middleware_records_http_red():
    client = TestClient(_hello_app())
    client.get("/")  # 触发一次 GET 200
    resp = client.get("/metrics")
    # GET + 2xx label 出现（HTTP RED Rate 信号，method/status_class 低基数）
    assert 'bycall_http_requests_total{method="GET",status_class="2xx"}' in resp.text
