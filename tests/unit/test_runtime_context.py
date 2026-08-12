"""Agent Runtime Context 仅描述当前治理结果。"""

from bycall.agent.runtime_context import build_runtime_context
from bycall.governance.capability import CapabilitySet
from bycall.governance.models import User
from bycall.tools.base import ToolSpec


def test_runtime_context_describes_identity_environment_and_filtered_tools():
    user = User(name="alice", display_name="Alice", role="member")
    capability = CapabilitySet(
        user_id=7,
        user_name="alice",
        role="member",
        level=2,
        allowed_tools=["calculator"],
        knowledge_scope_tags=["sales"],
        knowledge_scope_text="可访问知识域：sales（销售资料）",
        skills=["sales-playbook"],
    )
    allowed = ToolSpec(
        name="calculator",
        description="执行算术计算",
        parameters={"type": "object", "properties": {}},
        func=lambda: "0",
        requires_approval=True,
    )

    context = build_runtime_context(
        user=user,
        capability=capability,
        channel="wecom",
        conversation_id=42,
        tools=[allowed],
        has_history=True,
        has_summary=False,
        memory_count=1,
    )

    assert "用户名：alice" in context
    assert "显示名：Alice" in context
    assert "角色：member" in context
    assert "渠道：wecom" in context
    assert "多轮会话" in context
    assert "近期对话历史：可用" in context
    assert "管理员维护的用户长期记忆：1 条" in context
    assert "`calculator`：执行算术计算；执行前需要管理员审批" in context
    assert "sales-playbook" in context
    assert "system_prompt" not in context


def test_runtime_context_does_not_invent_unavailable_capabilities():
    user = User(name="bob", role="member")
    capability = CapabilitySet(user_id=8, user_name="bob", role="member")

    context = build_runtime_context(
        user=user,
        capability=capability,
        channel="api",
        conversation_id=None,
        tools=[],
        has_history=False,
        has_summary=False,
        memory_count=0,
    )

    assert "本轮可执行工具\n- 无" in context
    assert "当前未配置领域 Skill" in context
    assert "单轮请求" in context


def test_shared_message_preparation_keeps_sync_and_stream_context_identical(monkeypatch):
    """同步与流式入口共用同一准备函数，关键运行时事实不会漂移。"""
    from unittest.mock import MagicMock

    from bycall.agent.runner import _prepare_agent_messages

    user = User(id=9, name="carol", display_name="Carol", role="member")
    capability = CapabilitySet(
        user_id=9,
        user_name="carol",
        role="member",
        level=1,
        knowledge_scope_tags=["public"],
        knowledge_scope_text="只可访问 public",
    )
    session = MagicMock()
    monkeypatch.setattr(
        "bycall.agent.runner.load_user_memory", lambda session, user_id, question="": []
    )

    first = _prepare_agent_messages(
        session=session,
        user=user,
        cap=capability,
        question="你是谁？",
        channel="wecom",
        conversation_id=None,
        retrieved=[],
    )
    second = _prepare_agent_messages(
        session=session,
        user=user,
        cap=capability,
        question="你是谁？",
        channel="wecom",
        conversation_id=None,
        retrieved=[],
    )

    first_system = first[2][0].content
    second_system = second[2][0].content
    assert first_system == second_system
    assert "用户名：carol" in first_system
    assert "渠道：wecom" in first_system
    assert "只可访问 public" in first_system
