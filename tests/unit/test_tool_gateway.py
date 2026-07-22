"""工具网关单元测试 —— 验证 allowed_tools 白名单过滤（M3 工具治理核心论点）。

核心论点：LLM 的工具列表 = 「该角色可用工具集」，无权工具 LLM 根本看不到。
不是"看到不让调"，而是"根本不在列表里"。

策略活在代码层（filter_tools），LLM 碰不到——这是 product-design.md 支柱 3 的落地。
"""

from __future__ import annotations

import pytest

from bycall.governance.capability import CapabilitySet
from bycall.tools.base import ToolSpec
from bycall.tools.gateway import filter_tools, to_langchain_tools
from bycall.tools.registry import register


def _cap() -> CapabilitySet:
    """测试用默认能力集（gateway 只关心 cap 透传给 redactor）。"""
    return CapabilitySet(user_id=1, user_name="tester", role="member")


@pytest.fixture(autouse=True)
def _seed_tools():
    """每个用例前注册一组固定的测试工具。conftest 会在用例后清空。"""
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
            name="calc",
            description="计算器",
            parameters={
                "type": "object",
                "properties": {"expr": {"type": "string"}},
                "required": ["expr"],
            },
            func=lambda expr: "42",
        )
    )
    register(
        ToolSpec(
            name="send_email",
            description="发送邮件（高危）",
            parameters={
                "type": "object",
                "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                "required": ["to", "body"],
            },
            func=lambda to, body: "sent",
        )
    )


class TestFilterTools:
    """filter_tools 的白名单过滤逻辑。"""

    def test_admin_wildcard_gets_all(self):
        """admin 的 ['*'] 通配 → 全部已注册工具。"""
        filtered = filter_tools(["*"])
        assert {t.name for t in filtered} == {"get_time", "calc", "send_email"}

    def test_normal_user_only_sees_whitelisted(self):
        """普通用户只看到白名单内的工具，无权工具被排除。"""
        filtered = filter_tools(["get_time"])
        assert [t.name for t in filtered] == ["get_time"]

    def test_unauthorized_tool_not_in_list(self):
        """核心论点：无权工具（send_email）不在过滤结果里。"""
        filtered = filter_tools(["get_time", "calc"])
        names = {t.name for t in filtered}
        assert "send_email" not in names
        assert names == {"get_time", "calc"}

    def test_empty_whitelist_returns_empty(self):
        """空白名单 → 空列表（纯对话，不阻塞）。"""
        assert filter_tools([]) == []

    def test_unregistered_name_silently_ignored(self):
        """白名单里的未注册名静默忽略（admin 可能配了还没实现的工具名）。"""
        filtered = filter_tools(["get_time", "nonexistent_tool"])
        assert [t.name for t in filtered] == ["get_time"]

    def test_filtered_result_sorted_by_name(self):
        """过滤结果按 name 排序，保证输出稳定。"""
        # 故意乱序传入
        filtered = filter_tools(["send_email", "calc", "get_time", "*"])
        # admin 通配路径也走排序
        assert [t.name for t in filtered] == ["calc", "get_time", "send_email"]

    def test_prefix_wildcard_matches_mcp_server_tools(self):
        """``mcp__sales__*`` 匹配该 server 全部工具（server 加新工具自动可见）。"""
        # 注册几个 mcp 前缀工具（与 fixture 的内置工具共存）。
        register(
            ToolSpec(
                name="mcp__sales__query",
                description="",
                parameters={"type": "object", "properties": {}},
                func=lambda: "q",
                source="mcp",
            )
        )
        register(
            ToolSpec(
                name="mcp__sales__report",
                description="",
                parameters={"type": "object", "properties": {}},
                func=lambda: "r",
                source="mcp",
            )
        )
        register(
            ToolSpec(
                name="mcp__hr__lookup",
                description="",
                parameters={"type": "object", "properties": {}},
                func=lambda: "l",
                source="mcp",
            )
        )
        filtered = filter_tools(["mcp__sales__*"])
        assert {t.name for t in filtered} == {"mcp__sales__query", "mcp__sales__report"}

    def test_prefix_wildcard_and_exact_combined(self):
        """前缀通配 + 精确名混合放行。"""
        register(
            ToolSpec(
                name="mcp__sales__query",
                description="",
                parameters={"type": "object", "properties": {}},
                func=lambda: "q",
                source="mcp",
            )
        )
        filtered = filter_tools(["calc", "mcp__sales__*"])
        assert {t.name for t in filtered} == {"calc", "mcp__sales__query"}

    def test_bare_star_still_means_all(self):
        """单独 ``*`` 仍是全部（不是前缀匹配的退化）。"""
        register(
            ToolSpec(
                name="mcp__x__y",
                description="",
                parameters={"type": "object", "properties": {}},
                func=lambda: "z",
                source="mcp",
            )
        )
        filtered = filter_tools(["*"])
        assert "mcp__x__y" in {t.name for t in filtered}


class TestToLangchainTools:
    """to_langchain_tools 把 ToolSpec 包成 LangChain StructuredTool。"""

    def test_empty_specs_returns_empty(self):
        assert to_langchain_tools([], _cap()) == []

    def test_converts_to_base_tool(self):
        """转换结果都是 LangChain BaseTool，可被 ToolNode 消费。"""
        from langchain_core.tools import BaseTool

        filtered = filter_tools(["get_time"])
        lc_tools = to_langchain_tools(filtered, _cap())
        assert len(lc_tools) == 1
        assert isinstance(lc_tools[0], BaseTool)
        assert lc_tools[0].name == "get_time"
        assert lc_tools[0].description == "获取当前时间"

    def test_tool_executable_after_conversion(self):
        """转换后的 StructuredTool 仍能执行原 func。"""
        filtered = filter_tools(["get_time"])
        lc_tools = to_langchain_tools(filtered, _cap())
        # StructuredTool.invoke 用空 args 调用（get_time 无必填参数）。
        result = lc_tools[0].invoke({})
        assert result == "12:00"

    def test_tool_with_required_args(self):
        """带必填参数的工具转换后 args_schema 含 required 字段。"""
        filtered = filter_tools(["calc"])
        lc_tools = to_langchain_tools(filtered, _cap())
        result = lc_tools[0].invoke({"expr": "1+1"})
        assert result == "42"


def test_register_decorator():
    """@register_tool 装饰器把普通函数注册为工具。"""
    from bycall.tools.registry import register_tool

    @register_tool(
        "echo", "回声工具", {"type": "object", "properties": {"text": {"type": "string"}}}
    )
    def echo(text: str) -> str:
        return text

    from bycall.tools.registry import get_tool

    spec = get_tool("echo")
    assert spec is not None
    assert spec.name == "echo"
    assert spec.func("hello") == "hello"


class TestRedaction:
    """to_langchain_tools 的脱敏注入（product-design 支柱 4 第二条泄漏面）。

    核心论点：声明了 ``redactor`` 的工具，返回值经 ``redactor(raw, cap)`` 脱敏后才
    返回——这是工具结果进 LLM 上下文前的最后一道裁剪。无 redactor 的工具原样透传。
    """

    def test_redactor_applied_to_tool_output(self):
        """声明 redactor 的工具，返回值被脱敏（敏感串打码）。"""

        def redactor(raw, cap):
            return raw.replace("SECRET", "***") if cap.level < 10 else raw

        register(
            ToolSpec(
                name="lookup",
                description="查数据",
                parameters={"type": "object", "properties": {}},
                func=lambda: "row=SECRET-1234",
                redactor=redactor,
            )
        )
        cap = CapabilitySet(user_id=1, user_name="alice", role="staff", level=1)
        tools = to_langchain_tools(filter_tools(["lookup"]), cap)
        result = tools[0].invoke({})
        assert "SECRET" not in result
        assert "***" in result

    def test_no_redactor_passthrough(self):
        """无 redactor 的工具原样透传（默认不脱敏）。"""
        register(
            ToolSpec(
                name="plain",
                description="普通工具",
                parameters={"type": "object", "properties": {}},
                func=lambda: "raw-1234",
            )
        )
        tools = to_langchain_tools(filter_tools(["plain"]), _cap())
        assert tools[0].invoke({}) == "raw-1234"

    def test_redactor_receives_capability_set(self):
        """redactor 收到当前 cap，可按权限分级脱敏（admin 不脱敏）。"""

        def redactor(raw, cap):
            return "full" if cap.is_admin else "masked"

        register(
            ToolSpec(
                name="tiered",
                description="分级脱敏",
                parameters={"type": "object", "properties": {}},
                func=lambda: "data",
                redactor=redactor,
            )
        )
        tools_user = to_langchain_tools(
            filter_tools(["tiered"]),
            CapabilitySet(user_id=1, user_name="u", role="member"),
        )
        assert tools_user[0].invoke({}) == "masked"
        tools_admin = to_langchain_tools(
            filter_tools(["tiered"]),
            CapabilitySet(user_id=2, user_name="a", role="admin", is_admin=True),
        )
        assert tools_admin[0].invoke({}) == "full"
