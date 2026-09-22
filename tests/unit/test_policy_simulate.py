"""T1.8 策略模拟器测试：治理脊柱对「用户 + 动作」的判定可独立验证。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import create_engine

from bycall.admin.api import policy_simulate
from bycall.admin.schemas import PolicySimulateRequest
from bycall.config import get_settings
from bycall.governance.models import Agent, Role, RoleConfig, User
from bycall.tools.base import ToolSpec
from bycall.tools.registry import register


@pytest.fixture(autouse=True)
def _seed_tools():
    """按实例模拟需要真实工具规格（``filter_tools`` 只认已注册工具）。

    conftest 在每个用例后清空注册表，不存在跨用例污染。
    """
    for name in ("search_parts", "create_ticket"):
        register(
            ToolSpec(
                name=name,
                description=f"测试工具 {name}",
                parameters={"type": "object", "properties": {}},
                func=lambda: "ok",
            )
        )


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


def _add_agent(session, **kwargs) -> Agent:
    agent = Agent(name=kwargs.pop("name", "inv-bot"), **kwargs)
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def test_simulate_agent_scope_narrows_tools(db_session):
    """按实例模拟：实例白名单与角色取交集（只能收窄），并回带实例身份。"""
    u = _make_user(
        db_session, "erin", "erinrole", ["public"], 1, tools=["search_parts", "create_ticket"]
    )
    agent = _add_agent(db_session, allowed_tools=["search_parts"])

    allowed = policy_simulate(
        PolicySimulateRequest(
            user_id=u.id, action="tool_call", tool_name="search_parts", agent_id=agent.id
        ),
        db_session,
        None,
    )
    assert allowed.decision == "allowed"
    assert (allowed.agent_id, allowed.agent_name) == (agent.id, agent.name)

    # 角色允许但实例收窄 → 拒绝（实例永远只是天花板之下的收窄层）
    narrowed = policy_simulate(
        PolicySimulateRequest(
            user_id=u.id, action="tool_call", tool_name="create_ticket", agent_id=agent.id
        ),
        db_session,
        None,
    )
    assert narrowed.decision == "denied"
    assert "实例" in narrowed.detail


def test_simulate_agent_scope_unknown_agent_404(db_session):
    """实例不存在 → 404（不静默降级为角色基线）。"""
    u = _make_user(db_session, "frank", "frankrole", ["public"], 1)
    with pytest.raises(HTTPException) as exc:
        policy_simulate(PolicySimulateRequest(user_id=u.id, agent_id=987654), db_session, None)
    assert exc.value.status_code == 404


def test_simulate_agent_scope_not_visible_denied(db_session):
    """实例限定可见角色：名单外的角色 → denied（可见性先于能力集判定）。"""
    u = _make_user(db_session, "gina", "ginarole", ["public"], 1, tools=["search_parts"])
    agent = _add_agent(db_session, visible_roles=["other-role"])

    r = policy_simulate(PolicySimulateRequest(user_id=u.id, agent_id=agent.id), db_session, None)

    assert r.decision == "denied"
    assert "无权访问" in r.detail
