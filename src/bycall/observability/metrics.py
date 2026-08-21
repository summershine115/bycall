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

# ============ 阶段 2 · T2.5 五维运营指标 ============
# 1. 延迟：一轮完整对话（治理准备→LLM/工具回路→后处理）的耗时分布。
TURN_DURATION = Histogram(
    "bycall_turn_duration_seconds",
    "单轮对话端到端耗时分布（p50/p95/p99）",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20, 60),
)
# 2. 成本：token 用量与美元成本（估算），见 observability/usage.py。
# 3. 人工接管：审批请求创建 / 通过 / 拒绝（HITL 是当前唯一的人工接管路径）。
HUMAN_TAKEOVER = Counter(
    "bycall_human_takeover_total",
    "人工接管（HITL 审批）事件总数",
    ["kind"],  # kind: approval_requested/approved/rejected（低基数）
)
# 4. 失败：对话执行失败的轮次（LLM/工具回路异常）。
TURN_FAILURES = Counter(
    "bycall_turn_failures_total",
    "对话执行失败轮次数",
    ["kind"],  # kind: llm_error（低基数）
)
# 5. 重复执行：幂等去重拦截的重复消息（分子；processed = turns_total 作分母）。
DUPLICATE_MESSAGES = Counter(
    "bycall_duplicate_messages_total",
    "幂等去重拦截的重复消息数",
    ["layer"],  # layer: inflight/persisted（低基数）
)
# T3.3 授权免审执行次数（人工接管率的下降来源：session/always 档授权命中的直执行）。
APPROVAL_GRANT_BYPASS = Counter(
    "bycall_approval_grant_bypass_total",
    "审批授权（session/always 档）免审直接执行次数",
    ["scope"],  # scope: session/always（低基数）
)
# 对话轮次总数（按 decision 标签，供失败率/接管率做分母）。
TURNS_TOTAL = Counter(
    "bycall_turns_total",
    "对话轮次总数（按治理决策标签）",
    ["decision"],  # decision: answered/denied/denied_no_access/task/error（低基数）
)


def _family_samples(family_name: str) -> dict[str, float]:
    """从全局 REGISTRY 取某 metric family 的 sample 值表（sample_name -> value）。

    不依赖 ``generate_latest`` 文本解析；label 组合首次产生前为空表（值按 0 计）。
    """
    from prometheus_client import REGISTRY

    for metric in REGISTRY.collect():
        if metric.name == family_name:
            return {sample.name: sample.value for sample in metric.samples}
    return {}


def _counter_sum(family_name: str) -> float:
    """Counter family 全 label 组合求和（``<family>_total`` samples）。

    注意：同名 sample 会有多个 label 组合（如 decision=answered/error），不能按
    sample 名放进 dict（同名会互相覆盖），必须直接遍历 samples 累加。
    """
    from prometheus_client import REGISTRY

    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != family_name:
            continue
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total


def ops_metrics_snapshot() -> dict:
    """五维运营指标快照（T2.5 控制面看板数据源）。

    返回：延迟（轮次/均值秒）、成本（美元 + token）、人工接管率、失败率、重复执行率。
    所有 rate 的分母为 0 时返回 0（新部署还没有流量）。
    """
    turn_samples = _family_samples("bycall_turn_duration_seconds")
    turn_count = turn_samples.get("bycall_turn_duration_seconds_count", 0)
    turn_sum = turn_samples.get("bycall_turn_duration_seconds_sum", 0)

    from prometheus_client import REGISTRY

    import bycall.observability.usage  # noqa: F401  确保 token/cost family 已注册

    # usage 模块的 Counter 带 label，聚合 direction/kind 需要遍历 sample 对象。
    token_direction = {"input": 0.0, "output": 0.0}
    cost_total = 0.0
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == "bycall_llm_tokens_total":
                token_direction[sample.labels.get("direction", "")] += sample.value
            elif sample.name == "bycall_cost_total":
                cost_total += sample.value

    takeover = {"requested": 0.0, "approved": 0.0, "rejected": 0.0}
    kind_to_key = {
        "approval_requested": "requested",
        "approved": "approved",
        "rejected": "rejected",
    }
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == "bycall_human_takeover_total":
                key = kind_to_key.get(sample.labels.get("kind", ""))
                if key is not None:
                    takeover[key] += sample.value
    grant_bypass = _counter_sum("bycall_approval_grant_bypass")
    turns = _counter_sum("bycall_turns")
    failures = _counter_sum("bycall_turn_failures")
    duplicates = _counter_sum("bycall_duplicate_messages")

    def _rate(numerator: float, denominator: float) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "latency": {
            "turn_count": int(turn_count),
            "total_seconds": round(turn_sum, 4),
            "avg_seconds": round(turn_sum / turn_count, 4) if turn_count else 0.0,
        },
        "cost": {
            "total_usd": round(cost_total, 6),
            "input_tokens": int(token_direction["input"]),
            "output_tokens": int(token_direction["output"]),
        },
        "human_takeover": {
            "requested": int(takeover["requested"]),
            "approved": int(takeover["approved"]),
            "rejected": int(takeover["rejected"]),
            "grant_bypass": int(grant_bypass),
            "rate": _rate(takeover["requested"], turns),
        },
        "failure": {
            "failed_turns": int(failures),
            "total_turns": int(turns),
            "rate": _rate(failures, turns),
        },
        "duplicate_execution": {
            "blocked_messages": int(duplicates),
            "total_turns": int(turns),
            "rate": _rate(duplicates, duplicates + turns),
        },
    }
