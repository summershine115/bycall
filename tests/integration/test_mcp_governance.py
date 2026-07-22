"""MCP 工具网关端到端集成测试：MCP 工具贯穿治理脊柱管道。

验证 M4 Slice A 核心论点（红线 #5）：MCP server 的工具经 manager 注册进共享 _REGISTRY，
对治理管道透明——filter_tools 按白名单（含前缀通配）裁剪，to_langchain_tools 包成
StructuredTool 后 LLM 可调用。无需真实 LLM：直接 invoke StructuredTool 即证明链路通。
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from bycall.governance.capability import CapabilitySet
from bycall.mcp.client import MCPClient, MCPServerConfig
from bycall.mcp.manager import MCPManager
from bycall.tools.gateway import filter_tools, to_langchain_tools
from bycall.tools.registry import get_all_tools


def _make_calc_server() -> FastMCP:
    srv = FastMCP("calc")

    @srv.tool()
    def add(a: int, b: int) -> int:
        """加法"""
        return a + b

    return srv


def _factory(mcp_server: FastMCP):
    from mcp.shared.memory import create_connected_server_and_client_session

    def _make(config: MCPServerConfig) -> MCPClient:
        return MCPClient(
            config,
            session_cm_factory=lambda: create_connected_server_and_client_session(mcp_server),
            connect_timeout=15,
            call_timeout=10,
        )

    return _make


def _cap(allowed_tools: list[str]) -> CapabilitySet:
    return CapabilitySet(
        user_id=1,
        user_name="tester",
        role="sales",
        allowed_tools=allowed_tools,
    )


@pytest.fixture
def manager_with_calc():
    """注册一个 calc MCP server，工具进 _REGISTRY；用例后断开。"""
    from sqlmodel import Session, SQLModel, create_engine

    from bycall.governance.models import MCPServer

    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as session:
        srv = MCPServer(name="calc", transport="stdio", command="x", is_enabled=True)
        session.add(srv)
        session.commit()
        session.refresh(srv)
        mgr = MCPManager(client_factory=_factory(_make_calc_server()))
        mgr.load_all(session)
        yield mgr
        mgr.close_all()


def test_mcp_tool_visible_in_registry_and_tools_endpoint(manager_with_calc):
    """MCP 工具注册进 _REGISTRY，出现在工具目录，source=mcp。"""
    tools = {t.name: t for t in get_all_tools()}
    assert "mcp__calc__add" in tools
    assert tools["mcp__calc__add"].source == "mcp"


def test_prefix_wildcard_whitelist_exposes_mcp_tool(manager_with_calc):
    """角色白名单 mcp__calc__* → filter_tools 放行该 server 全部工具。"""
    filtered = filter_tools(["mcp__calc__*"])
    assert [t.name for t in filtered] == ["mcp__calc__add"]


def test_mcp_tool_invocable_through_langchain_pipeline(manager_with_calc):
    """端到端：filter_tools → to_langchain_tools → StructuredTool.invoke 返回正确结果。

    这是 graph.py ToolNode 实际消费工具的路径——证明 MCP 工具能被 LLM 调用。
    """
    cap = _cap(["mcp__calc__*"])
    specs = filter_tools(cap.allowed_tools)
    lc_tools = to_langchain_tools(specs, cap)
    assert len(lc_tools) == 1
    tool = lc_tools[0]
    assert tool.name == "mcp__calc__add"
    # invoke 按 args_schema 字段名传参（模拟 LLM tool_call）。
    result = tool.invoke({"a": 7, "b": 8})
    assert result == "15"


def test_role_without_whitelist_cannot_see_mcp_tool(manager_with_calc):
    """核心治理论点：未把 MCP 工具加进白名单的角色看不到它（LLM 看不到 = 不可调）。"""
    cap = _cap([])  # 空白名单
    assert filter_tools(cap.allowed_tools) == []


def test_mcp_tool_coexists_with_builtin(manager_with_calc):
    """MCP 工具与内置工具共存：admin 通配 * 同时拿到两者。"""
    from bycall.tools.base import ToolSpec
    from bycall.tools.registry import register

    register(
        ToolSpec(
            name="builtin_calc",
            description="",
            parameters={"type": "object", "properties": {}},
            func=lambda: "b",
        )
    )
    filtered = filter_tools(["*"])
    names = {t.name for t in filtered}
    assert "mcp__calc__add" in names
    assert "builtin_calc" in names
