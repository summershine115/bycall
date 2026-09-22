"""数字员工不预置 + 存量「默认助手」清理（ADR-0013）。

平台不再假装"组织本来就有一个员工"：``init_db`` 不灌任何 Agent，只把早期版本灌下的
种子足迹清掉。这里锁定两条不变量——新库零实例；升级只删"无人动过且无会话引用"的那条。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.models import Agent, Conversation, User

# 早期版本 _seed_defaults() 灌入的字段原样（清理判定的"种子足迹"基准）。
SEED_KWARGS = {
    "name": "default",
    "display_name": "默认助手",
    "description": "组织默认数字员工（开箱即用，可改名/改人格/改模型）",
    "channel": "api",
    "is_enabled": True,
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}
    )
    set_engine(engine)
    init_db()
    return engine


def _agents() -> list[tuple[int, str]]:
    """库里现有实例的 (id, display_name)——会话外取值，避开 detached 刷新。"""
    with session_scope() as s:
        return [(a.id, a.display_name) for a in s.exec(Agent.select_all()).all()]


def _add_seed_agent(**overrides) -> int:
    """按种子足迹插入一条 Agent（模拟早期版本已升级过的库）。"""
    with session_scope() as s:
        agent = Agent(**{**SEED_KWARGS, **overrides})
        s.add(agent)
        s.flush()
        return agent.id


def test_init_db_creates_no_agent(db):
    """新库零数字员工——"默认员工"不存在（产品约束）。"""
    assert _agents() == []


def test_cleanup_removes_untouched_seed(db):
    """存量升级：种子原样（未被改过、无会话引用）→ 清理，库里回到零实例。"""
    _add_seed_agent()
    assert len(_agents()) == 1

    init_db()

    assert _agents() == []


def test_cleanup_keeps_configured_instance(db):
    """管理员动过的同名实例不是种子足迹 → 保留（升级不误删用户数据）。"""
    agent_id = _add_seed_agent(display_name="库存助手")

    init_db()

    assert _agents() == [(agent_id, "库存助手")]


def test_cleanup_keeps_instance_with_conversations(db):
    """仍有会话引用 → 保留（历史归属不能悬空）。"""
    agent_id = _add_seed_agent()
    with session_scope() as s:
        user = User(name="u1", role="member", is_active=True)
        s.add(user)
        s.flush()
        s.add(Conversation(user_id=user.id, channel="api", agent_id=agent_id, session_key="api:u1"))

    init_db()

    assert [aid for aid, _ in _agents()] == [agent_id]


def test_cleanup_tolerates_legacy_is_default_column(db):
    """旧库仍带 is_default 列：升级不报错，种子足迹照样清掉。"""
    with db.begin() as conn:
        conn.execute(text('ALTER TABLE agent ADD COLUMN "is_default" BOOLEAN NOT NULL DEFAULT 1'))
    _add_seed_agent()

    init_db()

    assert _agents() == []
