"""请求级 correlation_id：contextvars 存储，贯穿日志/trace/审计。

与 capability token / approval context 一样走 contextvars——治理链路各处
（含 IM 渠道的后台线程桥接）都能取到当前请求的 id，实现「一次对话可复现」。
这是 on-call 问题 Q2（治理决策路径复现）的基础：凭 correlation_id 把一次请求
散落在各模块的日志/span/审计记录串成一条链。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

# 当前请求的 correlation_id。None = 未设置（如非 HTTP 上下文的 CLI / 后台任务）。
_correlation_id: ContextVar[str | None] = ContextVar("bycall_correlation_id", default=None)


def new_correlation_id() -> str:
    """生成新的 correlation_id（短 hex，便于日志/trace 肉眼关联）。"""
    return uuid.uuid4().hex[:16]


def get_correlation_id() -> str | None:
    """取当前上下文的 correlation_id（日志 filter / span attribute 用）。"""
    return _correlation_id.get()


def set_correlation_id(value: str | None):
    """设置 correlation_id，返回 token 供 reset_correlation_id 恢复。"""
    return _correlation_id.set(value)


def reset_correlation_id(token) -> None:
    """用 set_correlation_id 返回的 token 恢复上下文（请求结束必调，防泄漏）。"""
    _correlation_id.reset(token)
