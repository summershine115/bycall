"""运营效果评测用例（T2.1 第四类）。

离线样例覆盖 T2.5 五维指标的底层机制：
- 幂等去重（重复执行率的分母与分子来源）；
- 会话租约（并发串行化 → 稳定延迟/失败率）；
- Prometheus 五维指标注册（控制面看板数据源）。
"""

from __future__ import annotations

import uuid

from bycall.evals.core import EvalCase
from bycall.evals.harness import assert_true, eval_session


def _operations_idempotency_dedup(_ctx) -> None:
    """同一条消息只认领一次：inflight 与 DB 两层都拦重复（重复执行率=0 的机制）。"""
    from bycall.agent.idempotency import claim_message, mark_message_done, release_inflight

    key = f"eval:wecom:{uuid.uuid4().hex}"
    with eval_session() as session:
        first = claim_message(session, key)
        second = claim_message(session, key)
        assert_true(first, "首次到达的消息未被认领", key=key)
        assert_true(not second, "inflight 层的重复消息未被拦截", key=key)

        mark_message_done(session, key)
        session.commit()
        release_inflight(key)

        third = claim_message(session, key)
        assert_true(not third, "DB 持久层的重复消息未被拦截", key=key)


def _operations_session_lease_serializes(_ctx) -> None:
    """同一会话的租约互斥：并发消息不可能同时持有（防历史双写/乱序）。"""
    from bycall.agent.session_lease import acquire_lease, release_lease

    with eval_session() as session:
        token_a = acquire_lease(session, conversation_id=4242, holder="turn", ttl_seconds=60)
        assert_true(token_a is not None, "空闲会话未获取到租约")

        token_b = acquire_lease(session, conversation_id=4242, holder="turn", ttl_seconds=60)
        assert_true(token_b is None, "同一会话被第二个 holder 同时持有")

        assert_true(release_lease(session, 4242, token_a), "持 token 释放失败")
        token_c = acquire_lease(session, conversation_id=4242, holder="turn", ttl_seconds=60)
        assert_true(token_c is not None, "释放后应可再次获取", token=token_c)


def _operations_five_metrics_registered(_ctx) -> None:
    """T2.5 五维指标全部在 Prometheus 注册表（/metrics 可拉取）。

    Counter 的注册 family 名不带 ``_total``（那是 sample 名后缀）；断言 family 名即可。
    """
    from prometheus_client import REGISTRY

    import bycall.observability.usage  # noqa: F401  成本指标定义在这里，需显式导入

    registered = {metric.name for metric in REGISTRY.collect()}
    required = [
        "bycall_turn_duration_seconds",  # 延迟
        "bycall_cost",  # 成本（sample: bycall_cost_total）
        "bycall_human_takeover",  # 人工接管（sample: bycall_human_takeover_total）
        "bycall_turn_failures",  # 失败率（sample: bycall_turn_failures_total）
        "bycall_duplicate_messages",  # 重复执行率（sample: bycall_duplicate_messages_total）
    ]
    missing = [name for name in required if name not in registered]
    assert_true(not missing, "五维指标未全部注册", missing=missing, registered=sorted(registered))


def _operations_metrics_low_cardinality(_ctx) -> None:
    """指标 label 只允许低基数维度（path/user/错误原文是 cardinality 炸弹）。"""
    from prometheus_client import REGISTRY

    suspicious = ("path", "user_id", "error_msg", "question")
    offenders = []
    for metric in REGISTRY.collect():
        if metric.name.startswith("bycall_"):
            labels: set[str] = set()
            for sample in metric.samples:
                labels.update(sample.labels.keys())
            hit = [label for label in labels if label in suspicious]
            if hit:
                offenders.append((metric.name, hit))
    assert_true(not offenders, "指标存在高基数 label", offenders=offenders[:5])


CASES = [
    EvalCase(
        id="operations_idempotency_dedup",
        name="消息幂等去重",
        category="operations_effectiveness",
        description="同一条 IM 消息只认领一次（inflight + DB 双层），重复执行率为 0 的机制",
        run=_operations_idempotency_dedup,
        change_types=(),
    ),
    EvalCase(
        id="operations_session_lease_serializes",
        name="会话租约互斥",
        category="operations_effectiveness",
        description="同一会话同一时刻只有一个 holder，防历史双写/乱序",
        run=_operations_session_lease_serializes,
        change_types=(),
    ),
    EvalCase(
        id="operations_five_metrics_registered",
        name="五维运营指标注册",
        category="operations_effectiveness",
        description="延迟/成本/人工接管/失败/重复执行五维指标在 /metrics 可拉取",
        run=_operations_five_metrics_registered,
        change_types=(),
    ),
    EvalCase(
        id="operations_metrics_low_cardinality",
        name="指标低基数约束",
        category="operations_effectiveness",
        description="指标 label 不含 path/user/错误原文等高基数维度",
        run=_operations_metrics_low_cardinality,
        change_types=(),
    ),
]
