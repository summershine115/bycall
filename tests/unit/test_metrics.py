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


# ============ 外部依赖（embed/rerank wrapper）RED ============


def _llm_calls_value(kind: str, status: str) -> float:
    """从 /metrics 文本解析 bycall_llm_calls_total{kind,status} 当前值（累积）。"""
    from prometheus_client import generate_latest

    target = f'bycall_llm_calls_total{{kind="{kind}",status="{status}"}} '
    for line in generate_latest().decode().splitlines():
        if line.startswith(target):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def test_metered_embed_records_ok():
    from bycall.llm.base import EmbedModel
    from bycall.llm.resolver import _MeteredEmbedModel

    class FakeEmbed(EmbedModel):
        @property
        def dimension(self) -> int:
            return 4

        def embed(self, texts):
            return [[0.0] * 4 for _ in texts]

    before = _llm_calls_value("embed", "ok")
    _MeteredEmbedModel(FakeEmbed()).embed(["hi"])
    assert _llm_calls_value("embed", "ok") == before + 1


def test_metered_rerank_records_ok():
    from bycall.llm.base import RankResult, RerankModel
    from bycall.llm.resolver import _MeteredRerankModel

    class FakeRerank(RerankModel):
        @property
        def name(self) -> str:
            return "fake"

        def rerank(self, query, documents, top_n=None):
            return [RankResult(index=i, score=1.0) for i in range(len(documents))]

    before = _llm_calls_value("rerank", "ok")
    _MeteredRerankModel(FakeRerank()).rerank("q", ["a", "b"])
    assert _llm_calls_value("rerank", "ok") == before + 1


def test_metered_embed_records_error_on_failure():
    import pytest

    from bycall.llm.base import EmbedModel
    from bycall.llm.resolver import _MeteredEmbedModel

    class BoomEmbed(EmbedModel):
        @property
        def dimension(self) -> int:
            return 4

        def embed(self, texts):
            raise RuntimeError("boom")

    before = _llm_calls_value("embed", "error")
    with pytest.raises(RuntimeError):
        _MeteredEmbedModel(BoomEmbed()).embed(["x"])
    assert _llm_calls_value("embed", "error") == before + 1
