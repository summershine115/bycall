"""任务执行 typed event + 进程内事件总线（S1a T2）。

统一 ``run_agent`` / ``run_agent_stream`` 的事件产出（闭合实现缝隙 #6）——审计、流式输出、
（S1b）前端 SSE 都订阅同一事件流，避免两套 run 路径漂移。

设计要点：
- 事件是**事实记录**（发生了什么），不是命令。订阅者据此各自行动（写审计 / 推流 / 推前端）。
- 总线进程内**同步**派发（单 bot 单进程；跨进程 / 分布式留后置）。
- ``trace_id`` 预留接 OTel，链路追踪。
- handler 异常隔离：单个订阅者抛错不影响其他订阅者和发布方（事件链路中断比丢一条事件危险）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


TaskEventType = Literal[
    "task_created",
    "plan_produced",
    "step_started",
    "tool_called",
    "approval_requested",
    "step_completed",
    "step_failed",
    "task_done",
    "task_paused",
]
"""任务执行全程的事件类型（spec §3.3）。"""


class TaskEvent(BaseModel):
    """单条任务事件——执行过程的不可变事实记录。

    订阅者（审计 / 流式 / SSE）各取所需，互不耦合。``payload`` 是事件特定数据
    （如 ``tool_called`` 带 ``tool_name``/``args``/``result``），结构由事件类型决定。
    """

    type: TaskEventType
    task_id: int
    step_id: int | None = None
    run_id: int | None = None
    payload: dict[str, object] = Field(default_factory=dict)  # 事件特定数据
    trace_id: str | None = None  # OTel 链路追踪（预留）
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


EventHandler = Callable[[TaskEvent], None]


class TaskEventBus:
    """进程内事件总线——发布 / 订阅，同步派发。

    订阅者注册 handler；``publish`` 时按 type 派发给匹配订阅者 + 通配订阅者（``None`` = 全部）。
    handler 异常被隔离（记日志，不中断派发）。

    用法::

        bus.subscribe("step_completed", write_audit)  # 按类型
        bus.subscribe(None, log_all)                  # 通配（全部）
        bus.publish(event)
    """

    def __init__(self) -> None:
        self._handlers: dict[str | None, list[EventHandler]] = defaultdict(list)
        self._lock = Lock()

    def subscribe(self, event_type: str | None, handler: EventHandler) -> None:
        """订阅事件。``event_type=None`` 订阅全部（通配）。"""
        with self._lock:
            self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str | None, handler: EventHandler) -> None:
        """退订（测试 / 热插拔用）。"""
        with self._lock:
            handlers = self._handlers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def publish(self, event: TaskEvent) -> None:
        """同步派发：匹配类型 + 通配订阅者。handler 异常隔离。"""
        with self._lock:
            # 快照当前订阅者，避免派发期间订阅变更。
            handlers = list(self._handlers.get(event.type, [])) + list(self._handlers.get(None, []))
        for h in handlers:
            try:
                h(event)
            except Exception:
                logger.exception("task event handler failed: type=%s", event.type)


# 进程级默认总线（run_agent / run_agent_stream 共用，闭合缝隙 #6）。
default_bus = TaskEventBus()
