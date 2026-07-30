"""session 续跑测试（S1a T6）。

验证：paused Task（外部已批准）经 resume_pending_tasks 恢复完成；
running（中断）也能安全续跑（幂等）；无未完成任务返回空。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

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


def _task(user_id: int, goal: str = "续跑任务") -> int:
    from bycall.db import session_scope
    from bycall.governance.models import Task

    with session_scope() as s:
        t = Task(user_id=user_id, goal=goal)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


class TestResumePendingTasks:
    def test_resume_paused_completes(self, engine):
        """paused Task（外部已批准 step）经 resume 恢复完成。"""
        from bycall.agent.task_engine import resume_pending_tasks, run_task
        from bycall.db import session_scope
        from bycall.governance.models import Step, Task

        uid = _user()
        tid = _task(uid)
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
            )
            assert t.status == "paused"

        # 模拟外部批准 s1 → done
        with session_scope() as s:
            steps = list(s.exec(Step.select_by_task(tid)))
            s1 = next(st for st in steps if st.step_index == 1)
            s1.status = "done"
            s.add(s1)
            s.commit()

        # resume_pending_tasks 扫到 paused → 续跑 → completed
        with session_scope() as s:
            resumed = resume_pending_tasks(session=s, chat_model=_FakeChatModel("r2"))
            assert len(resumed) == 1
            assert s.get(Task, tid).status == "completed"

    def test_resume_running_interrupted(self, engine):
        """running（中断）Task 经 resume 安全续跑（幂等——done step 跳过）。"""
        from bycall.agent.task_engine import resume_pending_tasks, transition_task_status
        from bycall.db import session_scope
        from bycall.governance.models import Step, Task

        uid = _user()
        tid = _task(uid)
        # 手动构造 running Task（step0 done + step1 未完成），模拟中断
        with session_scope() as s:
            t = s.get(Task, tid)
            transition_task_status(s, t, "planning")
            transition_task_status(s, t, "running")
            s.add(
                Step(
                    task_id=tid,
                    step_index=0,
                    kind="tool_loop",
                    status="done",
                    spec_json='{"goal": "s0"}',
                    idempotency_key=f"task-{tid}-step-0",
                )
            )
            s.add(
                Step(
                    task_id=tid,
                    step_index=1,
                    kind="tool_loop",
                    spec_json='{"goal": "s1"}',
                    idempotency_key=f"task-{tid}-step-1",
                )
            )
            s.commit()

        # resume（扫到 running）→ 跳过 done step0，执行 step1 → completed
        with session_scope() as s:
            resumed = resume_pending_tasks(session=s, chat_model=_FakeChatModel("r1"))
            assert len(resumed) == 1
            assert s.get(Task, tid).status == "completed"

    def test_resume_no_pending_returns_empty(self, engine):
        """无 paused/running Task → 返回空列表。"""
        from bycall.agent.task_engine import resume_pending_tasks
        from bycall.db import session_scope

        with session_scope() as s:
            resumed = resume_pending_tasks(session=s, chat_model=_FakeChatModel())
            assert resumed == []
