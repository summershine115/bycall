"""外部事件入口（S1b）：``POST /api/events/{event_source}`` 触发任务。

对应 spec §3.5「事件触发」：admin 配置 ``TaskSchedule(trigger_type="event")`` 时指定
``event_source`` 作为外部系统的调用密钥（webhook path）。外部业务系统（Webhook / MCP
事件 / 业务系统回调）POST 到该路径即触发一次任务执行。

安全模型：
- ``event_source`` 本身就是凭据（类似 webhook URL 的随机路径），不另设 header；
- 事件触发的任务以 ``TaskSchedule.user_id`` 为执行身份，每步执行前重算能力集
  （红线 #7——权限变更即拒，调度不绕过治理）；
- 事件只能**触发**（创建 Task），不能注入任意指令——goal 来自 admin 配置的模板，
  外部 payload 仅填充 ``{payload.xxx}`` 占位符（缺键 → 400）。

无会话认证：调用方是外部系统而非登录用户，故不走 admin session 守卫。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


class EventTriggerBody(BaseModel):
    """事件触发请求体。``payload`` 填充模板 goal 的 ``{payload.xxx}`` 占位符。"""

    payload: dict = Field(default_factory=dict, description="事件载荷（模板占位符填充值）")


class EventTriggerResult(BaseModel):
    """事件触发结果（外部系统确认触发成功用）。"""

    task_id: int
    status: str  # 任务当前状态（draft 表示仅创建，等待后续执行）


@router.post("/{event_source}", response_model=EventTriggerResult)
def trigger_event(
    event_source: str,
    body: EventTriggerBody | None = None,
) -> EventTriggerResult:
    """外部事件入口：按 event_source 找到启用的调度，创建并执行任务。"""
    from bycall.agent.task_engine import fire_schedule_task
    from bycall.db import session_scope
    from bycall.governance.models import TaskSchedule
    from bycall.llm.resolver import get_chat_model

    with session_scope() as session:
        sched = session.exec(TaskSchedule.select_by_event_source(event_source)).first()
        if sched is None or not sched.is_enabled:
            # 未知来源/已停用：返回 404（不泄露调度是否存在）。
            raise HTTPException(status.HTTP_404_NOT_FOUND, "未知事件来源")

        resolved = get_chat_model(session)
        if resolved is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "未激活对话模型，无法执行任务",
            )
        _, chat_model = resolved

        try:
            task = fire_schedule_task(
                session=session,
                schedule=sched,
                payload=body.payload if body is not None else {},
                chat_model=chat_model,
            )
        except ValueError as e:
            # 模板缺 goal / payload 缺占位符字段 → 调用方请求不合法。
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

        sched.last_fired_at = datetime.now(UTC)
        session.add(sched)
        session.commit()
        logger.info("事件入口 %s 触发 → Task #%s", event_source, task.id)
        return EventTriggerResult(task_id=task.id, status=task.status)  # type: ignore[arg-type]
