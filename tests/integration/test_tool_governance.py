"""工具治理集成测试 —— 验证 allowed_tools 在 /chat 链路中真实生效（M3）。

核心论点：LLM 的工具列表 = 「该角色可用工具集」，无权工具 LLM 根本看不到。

测试覆盖：
1. 注册工具后，admin（['*']）的 graph 能调用任意工具。
2. 受限角色（allowed_tools=['get_time']）即使 LLM 请求调 send_email，
   graph 里根本没有 send_email（被 filter_tools 过滤），LLM 无法越权。
3. 工具调用被审计（AuditLog 的 detail 含工具名）。

用 mock LLM + 真实 LangGraph graph，验证治理链路而非 LLM 质量。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlmodel import create_engine, select

from bycall.agent.runner import run_agent
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.models import AuditLog, Role, RoleConfig, User
from bycall.governance.security import hash_password
from bycall.tools.base import ToolSpec
from bycall.tools.registry import register


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """干净环境：SQLite + dev 模式。"""
    db_file = tmp_path / "test_tool_gov.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_file}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False}))
    init_db()
    return tmp_path


@pytest.fixture(autouse=True)
def _seed_tools():
    """注册两个测试工具：一个普通（get_time），一个高危（send_email）。"""
    register(
        ToolSpec(
            name="get_time",
            description="获取当前时间",
            parameters={"type": "object", "properties": {}},
            func=lambda: "12:00",
        )
    )
    register(
        ToolSpec(
            name="send_email",
            description="发送邮件（高危，需要授权）",
            parameters={
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
            func=lambda to: "sent",
        )
    )


def _make_mock_llm_with_tool_call(tool_name: str, call_id: str = "call_1"):
    """构造 mock ChatModel：第一轮返回 tool_calls，第二轮返回最终文本。"""
    m = MagicMock(spec=BaseChatModel)
    m.bind_tools.return_value = m
    m.invoke.side_effect = [
        AIMessage(
            content="",
            tool_calls=[{"id": call_id, "name": tool_name, "args": {}, "type": "tool_call"}],
        ),
        AIMessage(content="基于工具的回答"),
    ]
    return m


def _make_user_with_role(session, name, role_name, allowed_tools, scope_tags):
    """建角色（含 allowed_tools）+ 用户，返回 user id。"""
    session.add(
        Role(
            name=role_name,
            description="测试角色",
            config=RoleConfig(
                allowed_tools=allowed_tools,
                knowledge_scope_tags=scope_tags,
                level=1,
            ),
        )
    )
    session.add(
        User(
            name=name,
            display_name=name,
            role=role_name,
            password_hash=hash_password("x"),
            is_active=True,
        )
    )
    session.commit()
    return session.exec(select(User).where(User.name == name)).first().id


# ============ run_agent 层面：工具治理生效 ============


class TestToolGovernanceInRunner:
    def test_authorized_tool_called_and_audited(self, env):
        """有权工具 → graph 执行工具调用，审计记录含工具名。"""
        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user_with_role(
                s, "alice", "staff", allowed_tools=["get_time"], scope_tags=["public"]
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        mock_llm = _make_mock_llm_with_tool_call("get_time")

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="几点了？",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "get_time"

        # 审计记录含工具名（detail 摘要）+ 结构化工具返回值（tool_results）
        with session_scope() as s:
            logs = s.exec(
                select(AuditLog).where(AuditLog.user_name == "alice", AuditLog.action == "chat")
            ).all()
            assert len(logs) >= 1
            log = logs[-1]
            assert "get_time" in log.detail
            # tool_results = [{name,args,result}] JSON，result 为工具返回值（本例无 redactor 原样）
            tool_records = json.loads(log.tool_results)
            assert len(tool_records) == 1
            assert tool_records[0]["name"] == "get_time"
            assert tool_records[0]["result"] == "12:00"

    def test_unauthorized_tool_not_in_graph(self, env):
        """核心论点：受限角色（无 send_email）即使 LLM 请求 send_email，graph 里没有它。

        这里 LLM 的 tool_calls 请求了 send_email，但 allowed_tools=['get_time']
        过滤后 graph 只绑定了 get_time。ToolNode 找不到 send_email 会报错，
        我们断言的是：graph 根本没让 send_email 进入工具集（通过 filter_tools 保证）。
        """
        from bycall.tools.gateway import filter_tools

        # 先验证过滤逻辑：send_email 不在过滤结果里
        filtered = filter_tools(["get_time"])
        assert "send_email" not in {t.name for t in filtered}

        # runner 路径：allowed_tools=['get_time'] → graph 只绑 get_time。
        # 即便 mock LLM 请求 send_email，ToolNode 找不到会回喂错误给 LLM，
        # LLM 二轮仍能给出回答（不崩）。
        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user_with_role(
                s, "bob", "intern", allowed_tools=["get_time"], scope_tags=["public"]
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        # LLM 第一轮请求 send_email（无权），第二轮给文本回答。
        mock_llm = MagicMock(spec=BaseChatModel)
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.invoke.side_effect = [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "send_email", "args": {"to": "x"}, "type": "tool_call"}
                ],
            ),
            AIMessage(content="我没有发邮件的权限"),
        ]

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="帮我发邮件",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        # 仍得到回答（不崩），但 send_email 未被授权执行
        assert result.decision == "answered"

        # 核心论点的直接证据：LLM 的 bind_tools 入参里根本没有 send_email。
        # graph 用 filter_tools 过滤后的列表 bind 给模型——无权工具"不在 LLM 的世界里"。
        assert mock_llm.bind_tools.called, "graph 应调用 bind_tools"
        bound = mock_llm.bind_tools.call_args[0][0]
        bound_names = {getattr(t, "name", t) for t in bound}
        assert "send_email" not in bound_names
        assert "get_time" in bound_names

    def test_unauthorized_tool_never_executed(self, env):
        """无权工具的 func 绝不被真正执行——不止是"不崩、answered"。

        ToolNode 对未知工具返回 error ToolMessage 而非抛错，所以只断言
        decision=="answered" 无法证明工具没被执行：万一回归导致 send_email
        真的进了工具集并被调用，上面的 test_unauthorized_tool_not_in_graph
        依然会通过。这里注册一个带副作用（置 flag + 返回 "SENT" 哨兵）的
        send_email，给角色白名单里不含它，驱动 mock LLM 先请求 send_email
        再给最终答案，断言 func 从未被触发、且没有任何 tool_calls 记录返回哨兵值。
        """
        called = {"sent": False}

        def _send_email(to):
            called["sent"] = True
            return "SENT"

        # 覆盖 _seed_tools 里的 send_email，换成带哨兵副作用的实现（register 是覆盖语义）。
        register(
            ToolSpec(
                name="send_email",
                description="发送邮件（高危，需要授权）",
                parameters={
                    "type": "object",
                    "properties": {"to": {"type": "string"}},
                    "required": ["to"],
                },
                func=_send_email,
            )
        )

        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user_with_role(
                s, "dave", "intern2", allowed_tools=["get_time"], scope_tags=["public"]
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        # LLM 第一轮请求 send_email（无权），第二轮给文本回答。
        mock_llm = MagicMock(spec=BaseChatModel)
        mock_llm.bind_tools.return_value = mock_llm
        mock_llm.invoke.side_effect = [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "send_email", "args": {"to": "x"}, "type": "tool_call"}
                ],
            ),
            AIMessage(content="我没有发邮件的权限"),
        ]

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="帮我发邮件",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        # 核心断言：send_email 的 func 从未被执行（治理在代码层拦下，而非 LLM 自觉）。
        assert called["sent"] is False
        # 且没有任何工具调用记录返回了哨兵值（即使 ToolNode 回喂了 error ToolMessage）。
        assert all(tc.get("result") != "SENT" for tc in result.tool_calls)

    def test_empty_allowed_tools_pure_conversation(self, env):
        """allowed_tools=[] → 无工具，退化为纯对话（graph 单次 invoke）。"""
        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user_with_role(
                s, "carol", "guest", allowed_tools=[], scope_tags=["public"]
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        # 无工具路径：LLM 直接返回文本，不 bind_tools。
        mock_llm = MagicMock(spec=BaseChatModel)
        mock_llm.invoke.return_value = AIMessage(content="纯对话回答")

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="你好",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        assert result.tool_calls == []
        # 无工具路径不应调 bind_tools
        mock_llm.bind_tools.assert_not_called()


# ============ 控制面：工具目录端点 ============


class TestToolsEndpoint:
    def test_list_tools_requires_admin_session(self, env):
        """GET /api/admin/tools 未登录 → 401。"""
        from bycall.api.app import create_app

        client = TestClient(create_app())
        resp = client.get("/api/admin/tools")
        assert resp.status_code == 401

    def test_list_tools_returns_directory(self, env):
        """admin 登录后能列出已注册工具目录。"""
        from bycall.api.app import create_app

        client = TestClient(create_app())
        resp = client.post("/api/login", data={"name": "admin", "password": "admin123"})
        assert resp.status_code == 200

        resp = client.get("/api/admin/tools")
        assert resp.status_code == 200
        tools = resp.json()
        names = {t["name"] for t in tools}
        assert "get_time" in names
        assert "send_email" in names
        # 验证 schema 字段
        email_tool = next(t for t in tools if t["name"] == "send_email")
        assert "description" in email_tool
        assert "parameters" in email_tool
