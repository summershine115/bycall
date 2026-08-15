"""五维运营指标测试（阶段 2 T2.5）。"""

from __future__ import annotations

import time

from bycall.observability.metrics import ops_metrics_snapshot


def test_record_llm_usage_updates_tokens_and_cost(monkeypatch):
    """token 用量与成本按单价估算入 Prometheus 计数器。"""
    from bycall.observability import usage

    usage.clear_cost_cache()
    monkeypatch.setattr(usage, "get_cost_rates", lambda: (1.0, 2.0))  # 每百万 token 美元
    before = ops_metrics_snapshot()
    usage.record_llm_usage("chat", 1_000_000, 500_000)
    after = ops_metrics_snapshot()

    assert after["cost"]["input_tokens"] == before["cost"]["input_tokens"] + 1_000_000
    assert after["cost"]["output_tokens"] == before["cost"]["output_tokens"] + 500_000
    # 1M × $1 + 0.5M × $2 = $2.00
    assert after["cost"]["total_usd"] >= before["cost"]["total_usd"] + 2.0


def test_ops_snapshot_has_five_dimensions():
    """五维快照结构完整（看板契约）。"""
    snap = ops_metrics_snapshot()
    assert set(snap) == {"latency", "cost", "human_takeover", "failure", "duplicate_execution"}
    assert "avg_seconds" in snap["latency"]
    assert "total_usd" in snap["cost"]
    assert "rate" in snap["human_takeover"]
    assert "rate" in snap["failure"]
    assert "rate" in snap["duplicate_execution"]


def test_turn_observation_updates_latency_and_failure():
    """runner 轮次观测：延迟计数 + 失败计数进入快照。"""
    from bycall.agent.runner import _observe_turn

    before = ops_metrics_snapshot()
    _observe_turn("answered", time.perf_counter() - 0.05)
    _observe_turn("error", time.perf_counter() - 0.01)
    after = ops_metrics_snapshot()

    assert after["latency"]["turn_count"] == before["latency"]["turn_count"] + 2
    assert after["failure"]["failed_turns"] == before["failure"]["failed_turns"] + 1
    assert after["failure"]["total_turns"] == before["failure"]["total_turns"] + 2


def test_idempotency_duplicate_metric():
    """重复消息被拦截时计入 duplicate counter（inflight / persisted 两层）。"""
    import uuid

    from bycall.agent.idempotency import claim_message, mark_message_done, release_inflight
    from bycall.evals.harness import eval_session

    key = f"ops-test:{uuid.uuid4().hex}"
    before = ops_metrics_snapshot()
    with eval_session() as session:
        assert claim_message(session, key) is True
        assert claim_message(session, key) is False  # inflight 层
        mark_message_done(session, key)
        session.commit()
        release_inflight(key)
        assert claim_message(session, key) is False  # persisted 层
    after = ops_metrics_snapshot()
    assert (
        after["duplicate_execution"]["blocked_messages"]
        == before["duplicate_execution"]["blocked_messages"] + 2
    )


def test_admin_ops_metrics_endpoint(tmp_path, monkeypatch):
    """控制面端点返回五维快照（前端看板数据源）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import create_engine

    from bycall.admin.api import router
    from bycall.config import get_settings
    from bycall.db import init_db, set_engine

    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/db.sqlite")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    set_engine(
        create_engine(f"sqlite:///{tmp_path}/db.sqlite", connect_args={"check_same_thread": False})
    )
    init_db()

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/api/admin/ops-metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"latency", "cost", "human_takeover", "failure", "duplicate_execution"}
