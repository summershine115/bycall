"""MCPClient 单元测试：sync↔async 桥 + 工具发现 + 调用 + 生命周期。

用 MCP SDK 内存传输（``create_connected_server_and_client_session`` + ``FastMCP``）
免子进程，隔离测试 MCPClient 的连接/发现/调用/关闭链路。
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from bycall.mcp.client import MCPClient, MCPServerConfig


def _make_calc_server() -> FastMCP:
    """最小 MCP server：一个 add 工具 + 一个 greet 工具。"""
    server = FastMCP("calc")

    @server.tool()
    def add(a: int, b: int) -> int:
        """两数相加"""
        return a + b

    @server.tool()
    def greet(name: str) -> str:
        """打招呼"""
        return f"hello {name}"

    return server


def _factory(server: FastMCP):
    """构造内存传输 factory（注入 MCPClient 用）。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    return lambda: create_connected_server_and_client_session(server)


def _connect(server: FastMCP, **kwargs) -> MCPClient:
    client = MCPClient(
        MCPServerConfig(name="calc", transport="stdio"),
        session_cm_factory=_factory(server),
        connect_timeout=15,
        call_timeout=10,
        **kwargs,
    )
    client.connect()
    return client


def test_connect_discovers_namespaced_tools():
    """连接后发现工具，名字空间化为 mcp__{server}__{tool}，source=mcp。"""
    client = _connect(_make_calc_server())
    try:
        names = sorted(t.name for t in client.tools)
        assert names == ["mcp__calc__add", "mcp__calc__greet"]
        assert all(t.source == "mcp" for t in client.tools)
        # 参数 schema 从 MCP inputSchema 来
        add_spec = next(t for t in client.tools if t.name.endswith("add"))
        assert "a" in add_spec.parameters["properties"]
        assert "b" in add_spec.parameters["properties"]
    finally:
        client.close()


def test_call_tool_returns_string_result():
    """func 同步调用 → 字符串结果。"""
    client = _connect(_make_calc_server())
    try:
        add_spec = next(t for t in client.tools if t.name.endswith("add"))
        assert add_spec.func(a=2, b=3) == "5"
        greet_spec = next(t for t in client.tools if t.name.endswith("greet"))
        assert greet_spec.func(name="alice") == "hello alice"
    finally:
        client.close()


def test_call_unknown_tool_returns_error_string_not_raise():
    """调用不存在的工具返回错误字符串，不抛异常（对齐 ToolSpec 契约）。"""
    client = _connect(_make_calc_server())
    try:
        result = client.call_tool("nope", {})
        assert isinstance(result, str)
        assert "nope" in result
    finally:
        client.close()


def test_call_after_close_returns_unavailable_string():
    """关闭后调用返回「不可用」字符串，不崩。"""
    client = _connect(_make_calc_server())
    add_spec = next(t for t in client.tools if t.name.endswith("add"))
    client.close()
    assert "不可用" in add_spec.func(a=1, b=1)


def test_connect_failure_propagates():
    """连接失败（factory 抛错）时 connect() 抛 RuntimeError。"""

    def bad_factory():
        raise RuntimeError("boom")

    client = MCPClient(
        MCPServerConfig(name="bad", transport="stdio"),
        session_cm_factory=bad_factory,
        connect_timeout=5,
    )
    with pytest.raises(RuntimeError, match="boom"):
        client.connect()
    client.close()


def test_close_is_idempotent():
    """重复 close 不报错。"""
    client = _connect(_make_calc_server())
    client.close()
    client.close()  # 幂等
