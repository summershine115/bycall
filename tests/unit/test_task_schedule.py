"""任务调度与计划拆解测试（S1b）。

覆盖：LLM 拆多步计划（合法 JSON / 垃圾输出降级单步 / 非法 kind 降级）；
goal 占位符插值（嵌套键、缺键 KeyError）；fire_schedule_task 按模板创建并执行任务。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.config import get_settings


class _FakeChatModel:
    """假 LLM：invoke 返回固定 AIMessage（可配置 JSON 计划或垃圾文本）。"""

    def __init__(self, content: str = "done"):
        self.content = content

    def invoke(self, messages):
        return AIMessage(content=self.content)

    def bind_tools(self, tools):
        return self


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()


def _user(name: str = "alice") -> int:
    from bycall.db import session_scope
    from bycall.governance.models import User
    from bycall.governance.security import hash_password

    with session_scope() as s:
        u = User(name=name, display_name=name, role="member", password_hash=hash_password("x"))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id  # type: ignore[return-value]


def _schedule(user_id: int, trigger_type: str = "cron", **kwargs) -> int:
    """建一条 TaskSchedule（直接落库），返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import TaskSchedule

    sched = TaskSchedule(
        name=kwargs.pop("name", f"sched-{trigger_type}"),
        trigger_type=trigger_type,
        user_id=user_id,
        **kwargs,
    )
    with session_scope() as s:
        s.add(sched)
        s.commit()
        s.refresh(sched)
        return sched.id  # type: ignore[return-value]


class TestPlanTaskGoal:
    def test_multi_step_plan_parsed(self, engine):
        """LLM 返回合法 JSON 计划 → 拆成多步（kind/goal 归一化）。"""
        from bycall.agent.task_engine import plan_task_goal

        llm = _FakeChatModel(
            content='[{"goal": "查故障", "kind": "tool_loop"}, {"goal": "建工单", "kind": "approval_wait"}]'
        )
        plan = plan_task_goal(llm, "排查并处理故障")
        assert plan == [
            {"goal": "查故障", "kind": "tool_loop"},
            {"goal": "建工单", "kind": "approval_wait"},
        ]

    def test_fence_wrapped_json_parsed(self, engine):
        """LLM 偶尔输出 Markdown 围栏包裹的 JSON → 剥离后正常解析。"""
        from bycall.agent.task_engine import plan_task_goal

        llm = _FakeChatModel(
            content='```json\n[{"goal": "单步任务", "kind": "tool_loop"}]\n```'
        )
        assert plan_task_goal(llm, "目标") == [{"goal": "单步任务", "kind": "tool_loop"}]

    def test_garbage_falls_back_to_single_step(self, engine):
        """LLM 输出垃圾（非 JSON）→ 降级单步（goal 作唯一 tool_loop 步）。"""
        from bycall.agent.task_engine import plan_task_goal

        llm = _FakeChatModel(content="好的，我来帮你处理这个任务！")
        assert plan_task_goal(llm, "目标") == [{"goal": "目标", "kind": "tool_loop"}]

    def test_illegal_kind_falls_back(self, engine):
        """计划含非法 kind（如 deterministic 未支持）→ 整体降级单步（可执行性优先）。"""
        from bycall.agent.task_engine import plan_task_goal

        llm = _FakeChatModel(content='[{"goal": "x", "kind": "deterministic"}]')
        assert plan_task_goal(llm, "目标") == [{"goal": "目标", "kind": "tool_loop"}]

    def test_empty_plan_falls_back(self, engine):
        """LLM 输出空数组 → 降级单步。"""
        from bycall.agent.task_engine import plan_task_goal

        llm = _FakeChatModel(content="[]")
        assert plan_task_goal(llm, "目标") == [{"goal": "目标", "kind": "tool_loop"}]


class TestInterpolateGoal:
    def test_nested_payload_placeholder(self, engine):
        from bycall.agent.task_engine import _interpolate_goal

        goal = "处理订单 {payload.order.id} 的退款"
        assert _interpolate_goal(goal, {"order": {"id": 42}}) == "处理订单 42 的退款"

    def test_missing_key_raises(self, engine):
        from bycall.agent.task_engine import _interpolate_goal

        with pytest.raises(KeyError):
            _interpolate_goal("订单 {payload.order.id}", {"order": {}})

    def test_non_payload_placeholder_kept(self, engine):
        from bycall.agent.task_engine import _interpolate_goal

        assert _interpolate_goal("目标 {goal}", {"goal": "x"}) == "目标 {goal}"


class TestFireScheduleTask:
    def test_cron_schedule_creates_and_runs(self, engine):
        """cron 调度触发：创建 Task（trigger=cron、schedule_id 关联）+ 执行完成。"""
        from bycall.agent.task_engine import fire_schedule_task
        from bycall.db import session_scope
        from bycall.governance.models import Task, TaskSchedule

        uid = _user()
        sid = _schedule(
            uid,
            task_template_json='{"goal": "每日巡检", "plan": [{"goal": "巡检", "kind": "tool_loop"}]}',
        )
        with session_scope() as s:
            sched = s.get(TaskSchedule, sid)
            task = fire_schedule_task(session=s, schedule=sched, chat_model=_FakeChatModel())
            assert task.status == "completed"
            assert task.trigger == "cron"
            assert task.schedule_id == sid
            assert task.user_id == uid
            assert task.goal == "每日巡检"
            assert s.get(Task, task.id) is not None

    def test_event_payload_interpolation(self, engine):
        """事件触发 payload 填充 goal 占位符。"""
        from bycall.agent.task_engine import fire_schedule_task
        from bycall.db import session_scope
        from bycall.governance.models import TaskSchedule

        uid = _user()
        sid = _schedule(
            uid,
            trigger_type="event",
            event_source="webhook-orders",
            task_template_json='{"goal": "处理订单 {payload.order_id}", "plan": []}',
        )
        with session_scope() as s:
            sched = s.get(TaskSchedule, sid)
            task = fire_schedule_task(
                session=s,
                schedule=sched,
                payload={"order_id": "A100"},
                chat_model=_FakeChatModel(),
            )
            assert task.goal == "处理订单 A100"
            assert task.trigger == "event"

    def test_event_payload_missing_key_raises(self, engine):
        """payload 缺占位符字段 → ValueError（事件端点据此 400）。"""
        from bycall.agent.task_engine import fire_schedule_task
        from bycall.db import session_scope
        from bycall.governance.models import TaskSchedule

        uid = _user()
        sid = _schedule(
            uid,
            trigger_type="event",
            event_source="webhook-orders",
            task_template_json='{"goal": "处理订单 {payload.order_id}", "plan": []}',
        )
        with session_scope() as s:
            sched = s.get(TaskSchedule, sid)
            with pytest.raises(ValueError, match="插值失败"):
                fire_schedule_task(session=s, schedule=sched, payload={})

    def test_no_chat_model_creates_draft(self, engine):
        """无 chat_model → 仅创建 draft Task（调度器跳过/事件端点 503）。"""
        from bycall.agent.task_engine import fire_schedule_task
        from bycall.db import session_scope
        from bycall.governance.models import TaskSchedule

        uid = _user()
        sid = _schedule(uid, task_template_json='{"goal": "巡检", "plan": []}')
        with session_scope() as s:
            sched = s.get(TaskSchedule, sid)
            task = fire_schedule_task(session=s, schedule=sched)
            assert task.status == "draft"

    def test_template_plan_validation(self, engine):
        """模板 plan 含非法项 → 过滤后执行；全非法 → LLM 重拆（fake 降级单步）。"""
        from bycall.agent.task_engine import fire_schedule_task
        from bycall.db import session_scope
        from bycall.governance.models import Step, TaskSchedule

        uid = _user()
        sid = _schedule(
            uid,
            task_template_json=(
                '{"goal": "巡检", "plan": [{"goal": "合法步", "kind": "tool_loop"}, '
                '{"goal": "非法步", "kind": "bogus"}]}'
            ),
        )
        with session_scope() as s:
            sched = s.get(TaskSchedule, sid)
            task = fire_schedule_task(session=s, schedule=sched, chat_model=_FakeChatModel())
            assert task.status == "completed"
            steps = list(s.exec(Step.select_by_task(task.id)))
            assert len(steps) == 1
            assert steps[0].kind == "tool_loop"
            assert steps[0].status == "done"
