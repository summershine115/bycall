"""cron 任务调度器（S1b）：进程内线程周期性扫描到期调度并触发任务。

对应 spec §3.5「定时触发」：admin 在控制面配置 ``TaskSchedule(trigger_type="cron")``，
本调度器按 cron 表达式在到期点创建 Task 并执行。与 ``ChannelManager`` 同款单例模式：
- 应用启动时 ``start()``，daemon 线程常驻；
- admin 增删改调度**无需重启**——每次 tick 重新查 DB（``TaskSchedule.select_enabled()``）；
- 触发走 ``fire_schedule_task``——与对话触发、事件触发共用同一执行引擎
  （Task/Step/Run + 每步重算能力集），差异只在「谁来创建 Task」。

治理语义：调度任务的执行身份是 ``TaskSchedule.user_id``（admin 配置），每步执行前仍按该
用户重算能力集——权限变更即拒（红线 #7），调度不绕过治理。

补触发语义：上次触发后错过的 cron 点会在下次 tick 补跑（``last_fired_at`` 只记录
成功触发时刻，触发失败下次 tick 自动重试）。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from croniter import croniter

logger = logging.getLogger(__name__)

# 默认 tick 间隔（秒）；可用 SystemSetting "task.scheduler_tick_seconds" 覆盖。
_DEFAULT_TICK_SECONDS = 30


def _tick_seconds() -> int:
    """调度器扫描间隔（SystemSetting 可配，默认 30s）。"""
    from bycall.db import session_scope
    from bycall.governance.models import SystemSetting

    try:
        with session_scope() as session:
            row = session.exec(SystemSetting.select_by_key("task.scheduler_tick_seconds")).first()
            if row is not None and row.value:
                return max(5, int(row.value))
    except Exception:  # DB 未就绪等场景 → 用默认值，不让调度器崩
        pass
    return _DEFAULT_TICK_SECONDS


class TaskScheduler:
    """cron 调度器单例：daemon 线程每 tick 扫描到期调度并触发。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动调度线程（幂等：已运行则忽略）。"""
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="task-scheduler", daemon=True)
            self._thread.start()
            logger.info("任务调度器已启动")

    def stop(self) -> None:
        """停止调度线程（幂等）。"""
        with self._lock:
            if self._thread is None:
                return
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None
            logger.info("任务调度器已停止")

    def _run(self) -> None:
        """主循环：tick → 等待 → 循环，直到 stop 信号。"""
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # 单次 tick 异常不杀死调度线程（记录后继续下一轮）。
                logger.exception("任务调度 tick 失败")
            self._stop.wait(_tick_seconds())

    def _tick(self) -> None:
        """扫描启用的 cron 调度，触发所有到期的（一次 tick 可补多个错过的点）。"""
        from bycall.agent.task_engine import fire_schedule_task
        from bycall.db import session_scope
        from bycall.governance.models import TaskSchedule
        from bycall.llm.resolver import get_chat_model

        now = datetime.now(UTC)
        with session_scope() as session:
            schedules = session.exec(TaskSchedule.select_enabled()).all()
            for sched in schedules:
                if sched.trigger_type != "cron" or not sched.cron_expr:
                    continue
                # 从上一次成功触发（或当前时间）算下一次 cron 点。
                base = sched.last_fired_at if sched.last_fired_at is not None else now
                try:
                    itr = croniter(sched.cron_expr, base, ret_type=datetime)
                    next_due = itr.get_next(datetime)
                except Exception:
                    logger.warning("调度 #%s cron 表达式计算失败: %s", sched.id, sched.cron_expr)
                    continue
                if next_due > now:
                    continue  # 未到期

                resolved = get_chat_model(session)
                if resolved is None:
                    # 无激活对话模型：跳过（下次 tick 仍到期 → 自动重试）。
                    logger.warning("无激活对话模型，跳过调度 #%s 触发", sched.id)
                    continue
                _, chat_model = resolved
                try:
                    task = fire_schedule_task(
                        session=session, schedule=sched, chat_model=chat_model
                    )
                    sched.last_fired_at = datetime.now(UTC)
                    session.add(sched)
                    session.commit()
                    logger.info("调度 #%s (%s) 触发 → Task #%s", sched.id, sched.name, task.id)
                except Exception:
                    # 触发失败：不更新 last_fired_at，下次 tick 自动重试。
                    logger.exception("调度 #%s (%s) 触发失败", sched.id, sched.name)


# 进程级单例（与 gateway/manager.ChannelManager 同模式）。
_scheduler = TaskScheduler()


def get_task_scheduler() -> TaskScheduler:
    """获取进程级调度器单例。"""
    return _scheduler
