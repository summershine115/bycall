"""能力集上下文：通过 contextvars 将当前 CapabilitySet 注入 MCP 工具调用。

问题：外部 MCP server 不知道"谁在调用"——它能拿到 LLM 生成的 tool_args，但拿不到
调用者的身份/角色/权限。这会限制 MCP server 自己做授权（如按用户 role 返回不同数据）。

解法：用 Python ``contextvars`` 在本请求的处理链路中携带当前 ``CapabilitySet``，
``MCPClient.call_tool`` 在发起 MCP 调用前读取它，把关键字段编码进 ``meta`` 字典，
由 MCP SDK 作为请求元数据传给 server。MCP server 从 ``ctx.request_context.meta``
读取（如 ``meta.bycall_role``）自行鉴权。

隔离保证：
- ``contextvars`` 天然按 ``asyncio.Task``/线程隔离——并发请求各自有独立作用域。
- ``meta`` 只含脱敏字段（user_id/role/level），不含密钥或提示词内容。
- 未设置时（如管理/测试路径）→ meta 为空 dict，MCP server 看到空元数据，按默认行为。
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bycall.governance.capability import CapabilitySet

# 每个请求的能力集——runner 在调用 graph 前设置，MCPClient.call_tool 读取。
_current_capability: contextvars.ContextVar[CapabilitySet | None] = contextvars.ContextVar(
    "bycall_current_capability", default=None
)


def set_current_capability(cap: CapabilitySet | None) -> contextvars.Token:
    """设置当前请求的能力集（返回 token，需在 finally 中 reset）。"""
    return _current_capability.set(cap)


def reset_current_capability(token: contextvars.Token) -> None:
    """恢复能力集到设置前的状态。"""
    _current_capability.reset(token)


def get_current_capability() -> CapabilitySet | None:
    """读取当前请求的能力集（MCPClient 调用时用）。"""
    return _current_capability.get(None)


def build_meta(cap: CapabilitySet) -> dict:
    """把 CapabilitySet 编码为 MCP call_tool 的 meta 负载。

    只传脱敏字段（不含密钥/提示词/secret），MCP server 自行鉴权。
    """
    return {
        "bycall_user_id": cap.user_id,
        "bycall_user_name": cap.user_name,
        "bycall_role": cap.role,
        "bycall_level": cap.level,
        "bycall_is_admin": cap.is_admin,
    }


__all__ = [
    "build_meta",
    "get_current_capability",
    "reset_current_capability",
    "set_current_capability",
]
