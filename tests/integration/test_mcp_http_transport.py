"""MCP streamable HTTP 传输回归测试：真实 server + 真实客户端，不走内存传输。

背景：``MCPClient`` 的 http 分支曾调用 ``streamable_http_client(url, headers=...)``，
而该函数（mcp ≥1.24 引入）只接受 ``http_client``——任何 streamable HTTP MCP server
都连不上（TypeError），却因所有既有测试都注入 ``session_cm_factory``（内存传输）而长期
未被发现。本用例起一个真实 FastMCP streamable-HTTP server，用生产的 ``MCPServerConfig``
配置连接，锁死「配置的 header 真的到达 server」这条链路。
"""

from __future__ import annotations

import socket
import threading

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from bycall.mcp.client import MCPClient, MCPServerConfig

_CUSTOM_HEADER = "x-bycall-test"


class _HeaderSpy:
    """ASGI 包装：记录每个 HTTP 请求的 header（断言自定义 header 真的到达 server）。"""

    def __init__(self, app):
        self.app = app
        self.headers: list[dict[str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.headers.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        await self.app(scope, receive, send)


def _make_calc_server() -> FastMCP:
    server = FastMCP("calc")

    @server.tool()
    def add(a: int, b: int) -> int:
        """两数相加"""
        return a + b

    return server


@pytest.fixture
def http_mcp_endpoint():
    """起一个真实 streamable-HTTP MCP server，返回 (url, header 观察器)。

    先把 socket 绑好再交给 uvicorn（``Server.run(sockets=...)``）：端口从绑定那刻起就在
    监听，客户端不必轮询等 server「就绪」，不会出现等多久才好的时序猜测。
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    spy = _HeaderSpy(_make_calc_server().streamable_http_app())
    server = uvicorn.Server(uvicorn.Config(spy, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/mcp", spy
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.mark.parametrize("headers", [{"X-Bycall-Test": "1"}, {}])
def test_http_transport_connects_and_calls(http_mcp_endpoint, headers: dict[str, str]):
    """http 传输能建连/发现工具/调用；配置的 header 随每个请求到达 server。"""
    url, spy = http_mcp_endpoint
    spy.headers.clear()

    client = MCPClient(
        MCPServerConfig(name="calc", transport="http", url=url, headers=headers),
        connect_timeout=15,
        call_timeout=10,
    )
    try:
        client.connect()
        assert [t.name for t in client.tools] == ["mcp__calc__add"]
        add_spec = next(t for t in client.tools if t.name.endswith("add"))
        assert add_spec.func(a=2, b=3) == "5"
    finally:
        client.close()

    assert spy.headers, "server 未收到任何 HTTP 请求"
    for seen in spy.headers:
        if headers:
            assert seen.get(_CUSTOM_HEADER) == "1"
        else:
            assert _CUSTOM_HEADER not in seen
