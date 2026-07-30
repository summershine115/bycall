"""任务状态机 + 编排引擎测试（S1a T4）。

覆盖：状态流转合法/非法、终态不可变；run_task 全 tool_loop 完成、遇审批暂停、
resume 幂等续跑、失败标 failed、终态不可执行。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.agent.events import TaskEventBus
from bycall.config import get_settings


class _FakeChatModel:
    def __init__(self, answer: str = "done"):
        self.answer = answer

    def invoke(self, messages):
        return AIMessage(content=self.answer)

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
        u = User(name=name, display_name=name, role="admin", password_hash=hash_password("x"))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _task(user_id: int, goal: str = "综合任务") -> int:
    from bycall.db import session_scope
    from bycall.governance.models import Task

    with session_scope() as s:
        t = Task(user_id=user_id, goal=goal)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


# ============ 状态机 ============


class TestStateMachine:
    def test_legal_transitions(self, engine):
        from bycall.agent.task_engine import transition_task_status
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        with session_scope() as s:
            t = s.get(Task, _task(uid))
            transition_task_status(s, t, "planning")
            assert t.status == "planning"
            transition_task_status(s, t, "running")
            assert t.status == "running"
            transition_task_status(s, t, "paused")
            assert t.status == "paused"
            transition_task_status(s, t, "running")
            assert t.status == "running"
            transition_task_status(s, t, "completed")
            assert t.status == "completed"

    def test_illegal_transition_raises(self, engine):
        """draft → running 非法（须先 planning）。"""
        from bycall.agent.task_engine import transition_task_status
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        with session_scope() as s:
            t = s.get(Task, _task(uid))
            with pytest.raises(ValueError, match="非法"):
                transition_task_status(s, t, "running")

    def test_terminal_immutable(self, engine):
        """终态不可再转。"""
        from bycall.agent.task_engine import transition_task_status
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        with session_scope() as s:
            t = s.get(Task, _task(uid))
            transition_task_status(s, t, "planning")
            transition_task_status(s, t, "running")
            transition_task_status(s, t, "completed")
            with pytest.raises(ValueError):
                transition_task_status(s, t, "running")


# ============ 编排引擎 ============


class TestRunTask:
    def test_all_tool_loop_completes(self, engine):
        """全 tool_loop plan → completed，事件齐全。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        bus = TaskEventBus()
        events = []
        bus.subscribe(None, events.append)

        with session_scope() as s:
            t = s.get(Task, tid)
            run_task(
                session=s,
                task=t,
                chat_model=_FakeChatModel("ok"),
                plan=[
                    {"goal": "查故障", "kind": "tool_loop"},
                    {"goal": "查备件", "kind": "tool_loop"},
                ],
                bus=bus,
            )
            assert t.status == "completed"

        types = [e.type for e in events]
        assert "task_created" in types
        assert "plan_produced" in types
        assert "task_done" in types

    def test_approval_wait_pauses(self, engine):
        """遇 approval_wait step → paused + approval_requested，未完成。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        bus = TaskEventBus()
        events = []
        bus.subscribe(None, events.append)

        with session_scope() as s:
            t = s.get(Task, tid)
            run_task(
                session=s,
                task=t,
                chat_model=_FakeChatModel(),
                plan=[
                    {"goal": "查", "kind": "tool_loop"},
                    {"goal": "审批删除", "kind": "approval_wait"},
                    {"goal": "确认", "kind": "tool_loop"},
                ],
                bus=bus,
            )
            assert t.status == "paused"

        types = [e.type for e in events]
        assert "approval_requested" in types
        assert "task_done" not in types

    def test_resume_skips_done(self, engine):
        """resume 跳过 done，从中断点继续（幂等——已执行 Step 不重跑）。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Run, Step, Task

        uid = _user()
        tid = _task(uid)
        bus = TaskEventBus()

        # 第一次：遇 approval_wait 暂停（step0 done, step1 pending, step2 未触）
        with session_scope() as s:
            t = s.get(Task, tid)
            run_task(
                session=s,
                task=t,
                chat_model=_FakeChatModel("r0"),
                plan=[
                    {"goal": "s0", "kind": "tool_loop"},
                    {"goal": "s1", "kind": "approval_wait"},
                    {"goal": "s2", "kind": "tool_loop"},
                ],
                bus=bus,
            )
            assert t.status == "paused"

        # 模拟外部批准：step1 → done
        with session_scope() as s:
            steps = list(s.exec(Step.select_by_task(tid)))
            s1 = next(st for st in steps if st.step_index == 1)
            s1.status = "done"
            s.add(s1)
            s.commit()

        # resume：再调 run_task（paused→running，跳过 done step0/step1，执行 step2）
        with session_scope() as s:
            t = s.get(Task, tid)
            run_task(session=s, task=t, chat_model=_FakeChatModel("r2"), plan=None, bus=bus)
            assert t.status == "completed"

        # 全部 done；step0 未因 resume 重跑（仍只有 1 个 Run）
        with session_scope() as s:
            steps = list(s.exec(Step.select_by_task(tid)))
            assert all(st.status == "done" for st in steps)
            s0 = next(st for st in steps if st.step_index == 0)
            assert len(list(s.exec(Run.select_by_step(s0.id)))) == 1

    def test_tool_loop_failure_marks_task_failed(self, engine):
        """tool_loop step 失败 → task failed。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Task

        class _Boom:
            def invoke(self, messages):
                raise RuntimeError("挂")

            def bind_tools(self, tools):
                return self

        uid = _user()
        tid = _task(uid)
        with session_scope() as s:
            t = s.get(Task, tid)
            run_task(
                session=s, task=t, chat_model=_Boom(), plan=[{"goal": "x", "kind": "tool_loop"}]
            )
            assert t.status == "failed"

    def test_plan_is_persisted(self, engine):
        """首次执行保存实际计划，供恢复与审计使用。"""
        import json

        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        plan = [{"goal": "查故障", "kind": "tool_loop"}]

        with session_scope() as s:
            task = s.get(Task, tid)
            run_task(session=s, task=task, chat_model=_FakeChatModel(), plan=plan)
            assert json.loads(task.plan_json) == plan

    def test_step_refreshes_capability_snapshot(self, engine):
        """每步执行前把最新能力集写回 Task，保留权限重验轨迹。"""
        import json

        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        with session_scope() as s:
            task = s.get(Task, tid)
            run_task(
                session=s,
                task=task,
                chat_model=_FakeChatModel(),
                plan=[{"goal": "查故障", "kind": "tool_loop"}],
            )
            snapshot = json.loads(task.capability_snapshot_json)
            assert snapshot["user_id"] == uid
            assert snapshot["role"] == "admin"
            assert snapshot["allowed_tools"] == ["*"]
            assert "system_prompt" not in snapshot
            assert "skill_markdown" not in snapshot
            assert "knowledge_scope_text" not in snapshot


    def test_empty_plan_fails_instead_of_completing(self, engine):
        """draft task 没有可执行计划时必须失败，不能零步完成。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        with session_scope() as s:
            task = s.get(Task, tid)
            with pytest.raises(ValueError, match="没有可执行步骤"):
                run_task(session=s, task=task, chat_model=_FakeChatModel(), plan=[])
            assert task.status == "failed"

    def test_running_task_without_steps_fails(self, engine):
        """损坏的 running task 不得被误标为 completed。"""
        from bycall.agent.task_engine import run_task, transition_task_status
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        with session_scope() as s:
            task = s.get(Task, tid)
            transition_task_status(s, task, "planning")
            transition_task_status(s, task, "running")
            with pytest.raises(ValueError, match="没有可执行步骤"):
                run_task(session=s, task=task, chat_model=_FakeChatModel())
            assert task.status == "failed"

    def test_unsupported_step_kind_is_rejected_before_side_effects(self, engine):
        """整份 plan 先校验；后置非法步骤不能让前置工具步骤先执行。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Run, Step, Task

        uid = _user()
        tid = _task(uid)
        with session_scope() as s:
            task = s.get(Task, tid)
            with pytest.raises(ValueError, match="不支持的 Step kind: deterministic"):
                run_task(
                    session=s,
                    task=task,
                    chat_model=_FakeChatModel(),
                    plan=[
                        {"goal": "先执行", "kind": "tool_loop"},
                        {"goal": "发推送", "kind": "deterministic"},
                    ],
                )
            assert task.status == "failed"
            steps = list(s.exec(Step.select_by_task(tid)))
            assert [step.status for step in steps] == ["pending", "failed"]
            assert list(s.exec(Run.select_by_step(steps[0].id))) == []

    def test_terminal_task_not_runnable(self, engine):
        """终态 task 再调 run_task 抛 ValueError。"""
        from bycall.agent.task_engine import run_task, transition_task_status
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        tid = _task(uid)
        with session_scope() as s:
            t = s.get(Task, tid)
            transition_task_status(s, t, "planning")
            transition_task_status(s, t, "running")
            transition_task_status(s, t, "completed")
            with pytest.raises(ValueError, match="不可执行"):
                run_task(session=s, task=t, chat_model=_FakeChatModel())
