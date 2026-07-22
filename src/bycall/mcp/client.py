"""MCPClient：一个 MCP server 的持久连接 + 工具发现 + 同步调用桥。

核心难题：MCP SDK 的 ``ClientSession.call_tool`` 是 async，而既有工具系统的
``ToolSpec.func`` 是 sync callable（LangGraph ``ToolNode`` 同步调用）。解法是每个
MCPClient 跑一个后台 daemon 线程的 asyncio loop，``ClientSession`` 在连接生命周期内
复用（不每次重连）；``ToolSpec.func`` 经 ``asyncio.run_coroutine_threadsafe`` 把异步
``call_tool`` 调度到后台 loop 阻塞等待结果，从而保持 ``ToolSpec.func`` 的 sync 契约不变。

生命周期：
  ``connect()``  → 后台线程起 loop → 建 transport + ClientSession + initialize + list_tools
                  → ``_ready`` 置位（成功）或记 ``_init_error``（失败）
  ``call_tool()`` → run_coroutine_threadsafe(session.call_tool) → 阻塞取结果（带超时）
  ``close()``    → 置 stop_event 让 lifespan 退出所有 async 上下文 → stop loop → join 线程

一个 server 挂掉只影响它自己的工具（其余 MCPClient / 内置工具照常）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from bycall.tools.base import ToolSpec

logger = logging.getLogger(__name__)

# 工具名前缀：mcp__{server_name}__{tool_name}。命名空间化，避免与内置工具冲突。
_TOOL_PREFIX = "mcp__"


@dataclass(frozen=True)
class MCPServerConfig:
    """MCPClient 的连接配置（与 DB 实体解耦——manager 负责解密 + 转换）。

    - ``transport="stdio"``：本地子进程，用 ``command``/``args``/``env``。
    - ``transport="http"``：streamable HTTP 远程 server，用 ``url``/``headers``。
    """

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class MCPClient:
    """一个 MCP server 的持久连接 + 工具发现 + 同步调用桥。

    线程模型：一个后台 daemon 线程独占一个 asyncio loop。``connect``/``call_tool``/
    ``close`` 均从调用线程经 ``run_coroutine_threadsafe``/``call_soon_threadsafe`` 跨线程
    操作 loop 上的协程与事件，保证 MCP 的 async 世界与 Bycall 的 sync 工具契约隔离。
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        connect_timeout: float = 30.0,
        call_timeout: float = 60.0,
        session_cm_factory: Any = None,
    ):
        """
        :param config: 连接配置。
        :param connect_timeout: 建连 + initialize + list_tools 的总超时（秒）。
        :param call_timeout: 单次 call_tool 的超时（秒）。
        :param session_cm_factory: 测试注入用——返回 async 上下文管理器（yield ClientSession）。
            生产为 None，按 config.transport 自建 stdio/http 连接。
        """
        self._config = config
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._session_cm_factory = session_cm_factory

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"mcp-{config.name}"
        )
        self._session: ClientSession | None = None
        self._tools: list[ToolSpec] = []
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None  # loop-bound，lifespan 内创建
        self._init_error: BaseException | None = None
        self._lifespan_future = None
        self._closed = False

    # ---------- 生命周期 ----------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def tools(self) -> list[ToolSpec]:
        """发现到的工具规格（``connect`` 成功后才可用）。"""
        return list(self._tools)

    def connect(self) -> None:
        """建连 + initialize + 发现工具。失败抛异常（连接错误/超时）。

        同步阻塞直到 ready 或超时。必须在首次 ``call_tool`` 前调用一次。
        """
        self._thread.start()
        self._lifespan_future = asyncio.run_coroutine_threadsafe(self._lifespan(), self._loop)
        if not self._ready.wait(timeout=self._connect_timeout):
            raise TimeoutError(
                f"MCP server '{self._config.name}' 连接超时（{self._connect_timeout}s）"
            )
        if self._init_error is not None:
            raise self._init_error
        logger.info("MCP server '%s' 已连接，发现 %d 个工具", self._config.name, len(self._tools))

    async def _lifespan(self) -> None:
        """连接生命周期：开 transport + session → initialize + discover → 持有到 close。"""
        self._stop_event = asyncio.Event()
        try:
            async with self._open_session() as session:
                await session.initialize()
                self._session = session
                resp = await session.list_tools()
                self._tools = [self._tool_to_spec(t) for t in resp.tools]
                self._ready.set()
                # 持有 session 直到 close() 置 stop_event。
                await self._stop_event.wait()
        except BaseException as e:  # lifespan 内任何失败都要让 connect 感知
            self._init_error = e
            self._ready.set()
            logger.warning("MCP server '%s' 连接失败: %s", self._config.name, e)
        finally:
            self._session = None

    @asynccontextmanager
    async def _open_session(self) -> AsyncIterator[ClientSession]:
        """按传输类型建连并 yield ClientSession（生命周期内复用）。"""
        if self._session_cm_factory is not None:
            # 测试路径：注入内存传输（已连好的 ClientSession）。
            async with self._session_cm_factory() as session:
                yield session
            return

        cfg = self._config
        if cfg.transport == "stdio":
            params = StdioServerParameters(
                command=cfg.command,
                args=list(cfg.args),
                env=dict(cfg.env) or None,
            )
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                yield session
        elif cfg.transport == "http":
            async with (
                streamable_http_client(cfg.url, headers=dict(cfg.headers) or None) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as session,
            ):
                yield session
        else:
            raise ValueError(f"未知传输类型: {cfg.transport!r}")

    # ---------- 同步调用桥 ----------

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """同步调用 MCP 工具，返回归一化的字符串结果。

        调度到后台 loop 的 ``session.call_tool`` 阻塞等待。超时或连接已关返回错误字符串
        （不抛异常——对齐 ``ToolSpec`` 约定：错误以字符串回喂 LLM 让其重试/降级）。
        """
        if self._session is None or self._closed:
            return f"[MCP 工具 {tool_name} 不可用：连接已关闭]"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(tool_name, arguments), self._loop
            )
            result = fut.result(timeout=self._call_timeout)
        except TimeoutError:
            logger.warning("MCP 工具 '%s/%s' 调用超时", self._config.name, tool_name)
            return f"[MCP 工具 {tool_name} 调用超时]"
        except Exception as e:  # 工具错误回喂字符串，不崩链路
            logger.warning("MCP 工具 '%s/%s' 调用失败: %s", self._config.name, tool_name, e)
            return f"[MCP 工具 {tool_name} 调用失败: {e}]"
        return _format_call_result(result)

    def close(self) -> None:
        """关闭连接：让 lifespan 退出所有 async 上下文 → 停 loop → join 线程。幂等。"""
        if self._closed:
            return
        self._closed = True
        # 跨线程置 stop_event（asyncio.Event 非线程安全，走 call_soon_threadsafe）。
        if self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        # 等 lifespan 协程退出（session/transport 上下文随之关闭）。
        if self._lifespan_future is not None:
            with suppress(BaseException):  # 关闭阶段不阻塞
                self._lifespan_future.result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        logger.info("MCP server '%s' 已断开", self._config.name)

    # ---------- 工具发现 → ToolSpec ----------

    def _tool_to_spec(self, tool: Any) -> ToolSpec:
        """把 MCP Tool 转成 ToolSpec（注册进共享 _REGISTRY，对治理管道透明）。"""
        full_name = f"{_TOOL_PREFIX}{self._config.name}__{tool.name}"
        params = (
            dict(tool.inputSchema) if tool.inputSchema else {"type": "object", "properties": {}}
        )
        description = tool.description or f"MCP 工具 {tool.name}（来自 {self._config.name}）"
        return ToolSpec(
            name=full_name,
            description=description,
            parameters=params,
            func=self._make_sync_func(tool.name),
            source="mcp",
        )

    def _make_sync_func(self, tool_name: str):
        """构造 sync func：闭包 tool_name，调度到后台 loop。StructuredTool 按 schema 反射
        调用（``**kwargs``），故签名用 ``**kwargs``。"""

        def _call(**kwargs: Any) -> str:
            return self.call_tool(tool_name, kwargs)

        return _call


def _format_call_result(result: Any) -> str:
    """把 MCP ``CallToolResult`` 归一成字符串（ToolMessage.content 透传 str）。

    - ``isError=True``：回喂错误说明（LLM 可据此重试）。
    - 多 content 块：拼 TextContent.text，非文本块降级为类型标注。
    """
    if getattr(result, "isError", False):
        # 服务端报错：提取文本，找不到则整体 str。
        return _extract_text(result) or f"[MCP 工具返回错误: {result}]"
    return _extract_text(result)


def _extract_text(result: Any) -> str:
    """从 CallToolResult.content 拼出文本。"""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        # TextContent 有 .text 字段；其他类型（image/resource）降级标注。
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            btype = getattr(block, "type", "unknown")
            parts.append(f"[非文本结果块: {btype}]")
    return "\n".join(parts) if parts else str(result)


__all__ = ["MCPClient", "MCPServerConfig"]
