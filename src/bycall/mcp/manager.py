"""MCPManager 单例：管理全部 MCPClient 的生命周期 + per-server 热重载。

镜像 ``gateway/manager.ChannelManager`` 的单例 + 锁 + reload 模式，差异在于管多个
client（一个 MCPServer 一个）。核心职责：

- ``load_all``：启动时读 enabled MCPServer → 解密配置 → 每个 MCPClient 连接 + 发现工具
  → 注册进 ``tools/registry._REGISTRY``（对治理管道透明）。
- ``reload(server_id)``：admin 改配置后只重启那一个 server（断旧 client + 建新 client +
  重新注册工具）。
- ``close_all``：应用关闭时断开全部。

一个 server 连接失败只影响它自己——记 warning 并跳过，其余 server 工具照常注册。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from sqlmodel import Session

from bycall.governance.models import MCPServer
from bycall.governance.secrets import decrypt_secret
from bycall.mcp.client import MCPClient, MCPServerConfig
from bycall.tools.registry import register, unregister

logger = logging.getLogger(__name__)


def _server_to_config(srv: MCPServer) -> MCPServerConfig:
    """MCPServer（DB 实体，凭证加密）→ MCPServerConfig（解密后的连接配置）。

    env / headers 整段 JSON 解密；解密失败降级为空（不让坏密钥炸掉整个 load）。
    """
    env: dict[str, str] = {}
    if srv.env_encrypted:
        try:
            env = json.loads(decrypt_secret(srv.env_encrypted))
        except (json.JSONDecodeError, RuntimeError):
            logger.warning("MCP server '%s' env 解密失败，降级为空", srv.name)
    headers: dict[str, str] = {}
    if srv.headers_encrypted:
        try:
            headers = json.loads(decrypt_secret(srv.headers_encrypted))
        except (json.JSONDecodeError, RuntimeError):
            logger.warning("MCP server '%s' headers 解密失败，降级为空", srv.name)
    args: list[str] = []
    if srv.args_json:
        try:
            args = list(json.loads(srv.args_json))
        except json.JSONDecodeError:
            logger.warning("MCP server '%s' args_json 解析失败，降级为空", srv.name)
    return MCPServerConfig(
        name=srv.name,
        transport=srv.transport,
        command=srv.command,
        args=args,
        env=env,
        url=srv.url,
        headers=headers,
    )


class MCPManager:
    """管理全部 MCPClient 的生命周期，支持 per-server 热重载。"""

    def __init__(self, client_factory=None) -> None:
        self._clients: dict[int, MCPClient] = {}  # server_id → client
        self._lock = threading.Lock()
        self._loaded = False
        # 生产为 None（按 config.transport 建真实 stdio/http 连接）；测试注入内存传输。
        self._client_factory = client_factory

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load_all(self, session: Session) -> list[dict]:
        """启动时连全部 enabled MCPServer，注册工具。返回每个 server 的连接结果。

        单个 server 失败只记 warning 跳过（不影响其余 server 与应用启动）。
        """
        with self._lock:
            servers = list(session.exec(MCPServer.select_enabled()).all())
            results = []
            for srv in servers:
                ok, msg = self._connect_one(srv)
                results.append({"name": srv.name, "ok": ok, "message": msg})
                if ok:
                    logger.info("MCP server '%s' 已加载", srv.name)
                else:
                    logger.warning("MCP server '%s' 加载失败: %s", srv.name, msg)
            self._loaded = True
            return results

    def reload(self, session: Session, server_id: int) -> tuple[bool, str]:
        """热重载单个 server：断开旧 client + 用最新配置建新连接 + 重新注册工具。

        admin 改完配置（CRUD）后调用，不重启 HTTP 服务。
        """
        with self._lock:
            # 1. 断开并摘除旧 client 的工具。
            old = self._clients.pop(server_id, None)
            if old is not None:
                self._unregister_client_tools(old)
                old.close()
            # 2. 读最新配置。
            srv = session.get(MCPServer, server_id)
            if srv is None:
                return False, "MCP server 不存在（可能已被删除）"
            if not srv.is_enabled:
                return True, f"MCP server '{srv.name}' 已禁用，工具已移除"
            # 3. 用新配置建连 + 注册。
            ok, msg = self._connect_one(srv)
            return ok, msg

    def disconnect(self, server_id: int) -> bool:
        """断开并摘除单个 server（delete 时用，不读 DB）。返回是否曾连接。"""
        with self._lock:
            old = self._clients.pop(server_id, None)
            if old is None:
                return False
            self._unregister_client_tools(old)
            old.close()
            return True

    def close_all(self) -> None:
        """应用关闭时断开全部 client。"""
        with self._lock:
            for client in self._clients.values():
                self._unregister_client_tools(client)
                client.close()
            self._clients.clear()
            self._loaded = False

    def status(self) -> list[dict]:
        """返回每个 MCP server 的连接状态（供 admin API）。"""
        with self._lock:
            return [
                {"name": c.name, "tool_count": len(c.tools), "connected": True}
                for c in self._clients.values()
            ]

    # ---------- 内部 ----------

    def _connect_one(self, srv: MCPServer) -> tuple[bool, str]:
        """对单个 server 建连 + 注册工具（调用方持锁）。失败不抛异常。"""
        config = _server_to_config(srv)
        client = None
        try:
            client = (
                self._client_factory(config)
                if self._client_factory is not None
                else MCPClient(config)
            )
            client.connect()
        except Exception as e:  # 单 server 失败不应阻断其余
            if client is not None:
                client.close()  # connect() 已起后台线程，失败时必须回收
            return False, f"连接失败: {e}"
        # 注册工具进共享 _REGISTRY（重名覆盖，方便热重载）。
        for spec in client.tools:
            register(spec)
        self._clients[srv.id] = client  # pyright: ignore[reportArgumentType]
        return True, f"已连接，发现 {len(client.tools)} 个工具"

    def _unregister_client_tools(self, client: MCPClient) -> None:
        """从 _REGISTRY 摘除该 client 注册的全部工具。"""
        for spec in client.tools:
            unregister(spec.name)


# ---- 单例（镜像 ChannelManager 约定）----

_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    """全局单例。"""
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


def reset_mcp_manager() -> None:
    """测试用：重置单例（需先 close_all 避免线程泄漏）。"""
    global _manager
    if _manager is not None:
        _manager.close_all()
    _manager = None


__all__: list[Any] = ["MCPManager", "get_mcp_manager", "reset_mcp_manager"]
