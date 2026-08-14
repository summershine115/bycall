"""T1.8 策略模拟器测试：治理脊柱对「用户 + 动作」的判定可独立验证。"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.admin.api import policy_simulate
from bycall.admin.schemas import PolicySimulateRequest
from bycall.config import get_settings
from bycall.governance.models import Role, RoleConfig, User


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, session_scope, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()
    with session_scope() as s:
        yield s


def _make_user(session, name, role, tags, level, tools=None):
    session.add(
        Role(
            name=role,
            config=RoleConfig(knowledge_scope_tags=tags, level=level, allowed_tools=tools or []),
        )
    )
    u = User(name=name, role=role, is_active=True)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_simulate_retrieve_allowed_with_filters(db_session):
    """retrieve 动作 → allowed + 召回前过滤条件（source IN + level <=）。"""
    u = _make_user(db_session, "alice", "sales", ["public", "sales"], 2)
    r = policy_simulate(PolicySimulateRequest(user_id=u.id, action="retrieve"), db_session, None)
    assert r.decision == "allowed"
    fields = {f["field"]: f for f in r.retrieval_filters}
    assert fields["source"]["values"] == ["public", "sales"]
    assert fields["level"]["op"] == "lte"
    assert fields["level"]["value"] == 2


def test_simulate_retrieve_denied_no_scope(db_session):
    """无可见知识域 → denied。"""
    u = _make_user(db_session, "bob", "empty", [], 0)
    r = policy_simulate(PolicySimulateRequest(user_id=u.id, action="retrieve"), db_session, None)
    assert r.decision == "denied"
    assert "无可见知识域" in r.detail


def test_simulate_tool_call_denied(db_session):
    """非白名单工具 → denied。"""
    u = _make_user(db_session, "carol", "staff", ["public"], 1)
    r = policy_simulate(
        PolicySimulateRequest(user_id=u.id, action="tool_call", tool_name="send_email"),
        db_session,
        None,
    )
    assert r.decision == "denied"
    assert "不在角色白名单" in r.detail


def test_simulate_disabled_user_denied(db_session):
    """禁用用户 → denied。"""
    u = _make_user(db_session, "dave", "davesales", ["public"], 1)
    u.is_active = False
    db_session.add(u)
    db_session.commit()
    r = policy_simulate(PolicySimulateRequest(user_id=u.id, action="retrieve"), db_session, None)
    assert r.decision == "denied"
