"""MCPManager 单元测试：生命周期 + 工具注册进共享 _REGISTRY + per-server 热重载。

用内存传输（client_factory 注入）免子进程；DB 用临时 SQLite + MCPServer 实体。
验证红线 #5：MCP 工具经 manager 注册进 tools/registry，对治理管道透明。
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from sqlmodel import Session, SQLModel, create_engine

from bycall.governance.models import MCPServer
from bycall.mcp.client import MCPClient, MCPServerConfig
from bycall.mcp.manager import MCPManager
from bycall.tools.registry import get_all_tools


def _make_server(name: str) -> FastMCP:
    """构造一个带 add 工具的内存 MCP server。"""
    srv = FastMCP(name)

    @srv.tool()
    def add(a: int, b: int) -> int:
        """加法"""
        return a + b

    return srv


def _factory_for(mcp_server: FastMCP):
    """client_factory：把 config 包成内存传输 MCPClient（忽略 transport）。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    def _make(config: MCPServerConfig) -> MCPClient:
        return MCPClient(
            config,
            session_cm_factory=lambda: create_connected_server_and_client_session(mcp_server),
            connect_timeout=15,
            call_timeout=10,
        )

    return _make


@pytest.fixture
def db_session(tmp_path):
    """临时 SQLite + 建表（仅 MCPServer 相关，够测 manager）。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _add_mcpserver(session, name, *, is_enabled=True):
    srv = MCPServer(name=name, transport="stdio", command="echo", is_enabled=is_enabled)
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return srv


def _mcp_tool_names():
    return {t.name for t in get_all_tools() if t.source == "mcp"}


def test_load_all_registers_tools_into_registry(db_session):
    """load_all 后 MCP 工具出现在共享 _REGISTRY，名字空间化。"""
    _add_mcpserver(db_session, "calc")
    mgr = MCPManager(client_factory=_factory_for(_make_server("calc")))
    try:
        results = mgr.load_all(db_session)
        assert results == [{"name": "calc", "ok": True, "message": "已连接，发现 1 个工具"}]
        assert _mcp_tool_names() == {"mcp__calc__add"}
    finally:
        mgr.close_all()


def test_load_all_skips_failed_server_keeps_others(db_session):
    """一个 server 连接失败只影响自己，其余照常注册。"""
    _add_mcpserver(db_session, "bad")
    _add_mcpserver(db_session, "good")

    def factory(config):
        if config.name == "bad":
            raise RuntimeError("boom")
        return _factory_for(_make_server("good"))(config)

    mgr = MCPManager(client_factory=factory)
    try:
        results = mgr.load_all(db_session)
        by_name = {r["name"]: r for r in results}
        assert by_name["bad"]["ok"] is False
        assert by_name["good"]["ok"] is True
        assert _mcp_tool_names() == {"mcp__good__add"}
    finally:
        mgr.close_all()


def test_reload_updates_tools(db_session):
    """reload(server_id) 断旧 client + 建新 client + 重新注册。"""
    srv = _add_mcpserver(db_session, "calc")
    mgr = MCPManager(client_factory=_factory_for(_make_server("calc")))
    try:
        mgr.load_all(db_session)
        assert _mcp_tool_names() == {"mcp__calc__add"}
        ok, msg = mgr.reload(db_session, srv.id)
        assert ok and "1 个工具" in msg
        assert _mcp_tool_names() == {"mcp__calc__add"}
    finally:
        mgr.close_all()


def test_reload_disabled_server_removes_tools(db_session):
    """禁用 server 后 reload → 其工具从注册表移除。"""
    srv = _add_mcpserver(db_session, "calc")
    mgr = MCPManager(client_factory=_factory_for(_make_server("calc")))
    try:
        mgr.load_all(db_session)
        assert _mcp_tool_names() == {"mcp__calc__add"}
        srv.is_enabled = False
        db_session.add(srv)
        db_session.commit()
        ok, msg = mgr.reload(db_session, srv.id)
        assert ok and "禁用" in msg
        assert _mcp_tool_names() == set()
    finally:
        mgr.close_all()


def test_close_all_removes_tools(db_session):
    """close_all 后 MCP 工具全部摘除。"""
    _add_mcpserver(db_session, "calc")
    mgr = MCPManager(client_factory=_factory_for(_make_server("calc")))
    mgr.load_all(db_session)
    assert _mcp_tool_names() == {"mcp__calc__add"}
    mgr.close_all()
    assert _mcp_tool_names() == set()
