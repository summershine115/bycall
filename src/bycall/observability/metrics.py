"""Prometheus metrics：RED 指标 + /metrics 端点。

与 OTel trace 平行——trace 看单请求「哪里慢」，metrics 看聚合「多频繁/多慢/多错」。
独立于 otel.enabled：始终 on（prometheus_client 轻量），/metrics 始终暴露。

label 严格低基数（method/status_class），**禁止** path/user_id/error_msg——
后者是 cardinality 炸弹，会让 metrics 后端撑爆（详见 observability skill「Metrics」节）。
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, make_asgi_app

# ============ HTTP RED（每个端点）============

HTTP_REQUESTS = Counter(
    "bycall_http_requests_total",
    "HTTP 请求总数",
    ["method", "status_class"],  # status_class: 2xx/4xx/5xx（低基数，非 raw status）
)
HTTP_REQUEST_DURATION = Histogram(
    "bycall_http_request_duration_seconds",
    "HTTP 请求耗时分布（p50/p95/p99 可查）",
    ["method"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# ============ 治理链路 ============

# RAG 检索（on-call Q3：召回数分布 + 检索耗时）
RAG_RETRIEVAL_DURATION = Histogram(
    "bycall_rag_retrieval_duration_seconds",
    "RAG 跨 KB 检索耗时分布",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
RAG_RETRIEVED_CHUNKS = Histogram(
    "bycall_rag_retrieved_chunks",
    "RAG 检索召回 chunk 数分布（0 = ACL 全过滤或无命中）",
    buckets=(0, 1, 3, 5, 10, 20, 50, 100),
)
# ============ 能力集缓存（M5 性能调优）============

# compute_capability_set 每条消息都会跑（5+ 次 DB 查询），M5 加了 user_id 级缓存。
# hit/miss 比例让 on-call 验证缓存确实生效（miss 占比高 = 失效过于频繁或未命中）。
CAPABILITY_CACHE = Counter(
    "bycall_capability_cache_total",
    "能力集缓存命中/未命中计数（验证缓存生效、定位失效频率）",
    ["result"],  # result: hit/miss（低基数）
)


def status_class(status: int) -> str:
    """HTTP 状态码 → 类别（2xx/4xx/5xx），保持 label 低基数。"""
    return f"{status // 100}xx"


def metrics_asgi_app():
    """返回 /metrics ASGI app（Prometheus pull 端点，prometheus_client 提供）。"""
    return make_asgi_app()


# ============ 外部依赖（LLM/embed/rerank 调用，on-call Q4）============

LLM_CALLS = Counter(
    "bycall_llm_calls_total",
    "外部 LLM/embed/rerank 调用总数",
    ["kind", "status"],  # kind: chat/embed/rerank；status: ok/error（低基数）
)
LLM_CALL_DURATION = Histogram(
    "bycall_llm_call_duration_seconds",
    "外部 LLM/embed/rerank 调用耗时分布",
    ["kind"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
