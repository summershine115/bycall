"""内置工具端到端测试 —— 验证 M3 工具回路用真实工具跑通（M3 收尾）。

三个工具各验证工具回路的一个特性：
1. ``current_time``：无参工具能进回路并执行（最简形态）。
2. ``calculator``：带必填参数的工具——schema 反射调用 + 安全求值（注入防御）。
3. ``whoami``：脱敏 + CapabilitySet 闭包注入（支柱 4 第二条泄漏面）。

外加「注册触发」测试：``import bycall.tools`` 后内置工具自动出现在注册表与
``GET /api/admin/tools`` 控制面目录里。

范式与 ``test_tool_governance.py`` 一致：mock LLM（spec=BaseChatModel）+ 真 LangGraph
graph + SQLite，验证治理链路而非 LLM 质量。

⚠️ conftest 的 ``_reset_settings`` autouse fixture 会在每个用例前后 ``clear_registry()``，
而 ``import bycall.tools.builtins`` 只在首次 import 执行顶层 register（模块缓存）。故
用例需用 ``_reload_builtins`` fixture 重新触发注册——这同时也验证了「import 即注册」
的接入点（``tools/__init__.py`` 末尾的 import 钩子）。
"""

from __future__ import annotations

import importlib
import json
import re
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
from bycall.governance.models import Role, RoleConfig, User
from bycall.governance.security import hash_password


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """干净环境：SQLite + dev 模式。"""
    db_file = tmp_path / "test_builtin.db"
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
def _reload_builtins():
    """重新触发 builtins 顶层 register（conftest 已在用例前 clear_registry）。

    ``importlib.reload`` 重跑 ``builtins.py`` 顶层代码，重新执行三条 ``register(...)``，
    把内置工具放回注册表。这也验证了接入点：内置工具靠 import 注册，reload 等价于
    "首次 import"。
    """
    import bycall.tools.builtins

    importlib.reload(bycall.tools.builtins)
    yield


def _make_mock_llm_with_tool_call(
    tool_name: str, args: dict, call_id: str = "call_1", final_text: str = "工具调用完成"
):
    """构造 mock ChatModel：第一轮返回 tool_calls，第二轮返回最终文本。"""
    m = MagicMock(spec=BaseChatModel)
    m.bind_tools.return_value = m
    m.invoke.side_effect = [
        AIMessage(
            content="",
            tool_calls=[{"id": call_id, "name": tool_name, "args": args, "type": "tool_call"}],
        ),
        AIMessage(content=final_text),
    ]
    return m


def _make_user(session, name, role_name, allowed_tools, scope_tags, level=1):
    """建角色（含 allowed_tools）+ 用户，返回 user id。"""
    session.add(
        Role(
            name=role_name,
            description="测试角色",
            config=RoleConfig(
                allowed_tools=allowed_tools,
                knowledge_scope_tags=scope_tags,
                level=level,
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


# ============ 1. 注册触发：import 即注册 ============


class TestRegistrationTrigger:
    def test_builtin_tools_registered_on_import(self):
        """reload builtins 后，三个内置工具出现在注册表。"""
        from bycall.tools.registry import get_all_tools

        names = {t.name for t in get_all_tools()}
        assert {"current_time", "calculator", "whoami"} <= names

    def test_admin_tools_endpoint_lists_builtins(self, env):
        """admin 登录后 GET /api/admin/tools 能列出内置工具目录。"""
        from bycall.api.app import create_app

        client = TestClient(create_app())
        client.post("/api/login", data={"name": "admin", "password": "admin123"})

        resp = client.get("/api/admin/tools")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert {"current_time", "calculator", "whoami"} <= names

        # 验证 schema 字段（calculator 带必填 expression 参数）
        calc = next(t for t in resp.json() if t["name"] == "calculator")
        assert "expression" in calc["parameters"]["properties"]
        assert calc["parameters"]["required"] == ["expression"]


# ============ 2. calculator：带参工具 + 注入防御 ============


class TestCalculator:
    def test_calculator_executed_through_loop(self, env):
        """calculator 带 expression 参数经 graph 执行，结果进审计。"""
        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user(
                s, "alice", "staff", allowed_tools=["calculator"], scope_tags=["public"]
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        mock_llm = _make_mock_llm_with_tool_call("calculator", {"expression": "1 + 2 * 3"})

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="算一下 1+2*3",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "calculator"
        # 工具返回值进审计：1+2*3 = 7
        assert result.tool_calls[0]["result"] == "7"

    def test_calculator_rejects_injection(self):
        """calculator 用 AST 白名单拒绝代码注入，返回错误说明而非抛异常。"""
        from bycall.tools.registry import get_tool

        calc = get_tool("calculator")
        assert calc is not None
        # 经典注入 payload：__import__ 调用 → AST Call 节点 → 拒绝
        result = calc.func("__import__('os').system('rm -rf /')")
        assert "不允许" in result or "错误" in result
        # 名字引用 → AST Name 节点 → 拒绝
        assert "不允许" in calc.func("open")
        # 除零 → 运行时兜底，返回错误说明
        assert "除零" in calc.func("1/0")

    def test_calculator_arithmetic_variants(self):
        """calculator 覆盖各种合法算术 + 整数结果去 .0 后缀。"""
        from bycall.tools.registry import get_tool

        calc = get_tool("calculator")
        assert calc.func("2 ** 10") == "1024"
        assert calc.func("(100 - 30) // 2") == "35"
        assert calc.func("7 % 3") == "1"
        # 整数结果去 .0：4/2 → "2" 而非 "2.0"
        assert calc.func("4 / 2") == "2"
        # 非整数保留：7/2 → "3.5"
        assert calc.func("7 / 2") == "3.5"


# ============ 3. whoami：脱敏 + CapabilitySet 闭包注入（支柱 4 第二条泄漏面）============


class TestWhoamiRedaction:
    def test_whoami_masks_user_id_for_normal_user(self, env):
        """普通用户调 whoami → user_id 打码（不暴露内部主键给非 admin）。"""
        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user(
                s, "bob", "staff", allowed_tools=["whoami"], scope_tags=["public"], level=1
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        mock_llm = _make_mock_llm_with_tool_call("whoami", {})

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="我是谁？",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        whoami_result = result.tool_calls[0]["result"]
        # 普通用户：user_id 被打码
        info = json.loads(whoami_result)
        assert info["user_id"] == "***"
        assert info["user_name"] == "bob"
        assert "allowed_tools" not in info  # 普通用户不暴露完整工具白名单

    def test_whoami_shows_full_for_admin(self, env):
        """admin 调 whoami → user_id 明文 + 含 allowed_tools（脱敏按权限分级）。"""
        mock_llm = _make_mock_llm_with_tool_call("whoami", {})

        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            admin = s.exec(select(User).where(User.name == "admin")).first()
            assert admin is not None, "dev 模式应内置 admin 用户"
            conv = get_or_create_conversation(s, admin.id, "api")
            conv_id = conv.id

        with session_scope() as s:
            admin = s.exec(select(User).where(User.name == "admin")).first()
            result = run_agent(
                session=s,
                user=admin,
                question="我是谁？",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        info = json.loads(result.tool_calls[0]["result"])
        # admin：user_id 明文 + 含 allowed_tools
        assert isinstance(info["user_id"], int)
        assert info["allowed_tools"] == ["*"]
        assert info["user_name"] == "admin"


# ============ 4. current_time：无参工具进回路 ============


class TestCurrentTime:
    def test_current_time_through_loop(self, env):
        """current_time 无参工具经 graph 执行，返回 ISO 时间戳。"""
        with session_scope() as s:
            from bycall.agent.session_store import get_or_create_conversation

            user_id = _make_user(
                s, "carol", "staff", allowed_tools=["current_time"], scope_tags=["public"]
            )
            conv = get_or_create_conversation(s, user_id, "api")
            conv_id = conv.id

        mock_llm = _make_mock_llm_with_tool_call("current_time", {})

        with session_scope() as s:
            user = s.get(User, user_id)
            result = run_agent(
                session=s,
                user=user,
                question="现在几点？",
                llm=mock_llm,
                channel="api",
                conversation_id=conv_id,
            )

        assert result.decision == "answered"
        ts = result.tool_calls[0]["result"]
        # ISO 8601 带时区偏移（如 2026-07-16T09:32:06...+00:00）
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)
