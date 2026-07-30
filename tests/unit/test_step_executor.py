"""Step 执行器测试（S1a T3）。

用 fake chat_model 跑通 run_tool_loop（空工具路径：单次 invoke），验证：
- 成功：Run.status=ok + step.done + step_started/completed 事件
- 权限拒绝（用户禁用）：Run.status=error + step.failed + step_failed（重算能力集生效）
- 执行异常：Run.status=error + step.failed + step_failed

脱敏由 to_langchain_tools 内部保证（test_tool_gateway 已覆盖），此处不重复测。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.agent.events import TaskEventBus
from bycall.config import get_settings


class _FakeChatModel:
    """假 chat model：invoke 返回固定 AIMessage（无 tool_calls → run_tool_loop 单轮结束）。"""

    def __init__(self, answer: str = "执行完成"):
        self.answer = answer

    def invoke(self, messages):
        return AIMessage(content=self.answer)

    def bind_tools(self, tools):
        return self


class _BoomChatModel:
    """假 chat model：invoke 抛错，模拟 run_tool_loop 执行失败。"""

    def invoke(self, messages):
        raise RuntimeError("LLM 挂了")

    def bind_tools(self, tools):
        return self


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()


def _create_user(name: str, role: str = "admin", is_active: bool = True) -> int:
    from bycall.db import session_scope
    from bycall.governance.models import User
    from bycall.governance.security import hash_password

    with session_scope() as s:
        u = User(
            name=name,
            display_name=name,
            role=role,
            password_hash=hash_password("x"),
            is_active=is_active,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _create_task_step(user_id: int, goal: str = "查销量"):
    from bycall.db import session_scope
    from bycall.governance.models import Step, Task

    with session_scope() as s:
        t = Task(user_id=user_id, goal=goal)
        s.add(t)
        s.commit()
        s.refresh(t)
        st = Step(task_id=t.id, step_index=0, kind="tool_loop")
        s.add(st)
        s.commit()
        s.refresh(st)
        return t.id, st.id


class TestExecuteToolLoopStep:
    def test_success(self, engine):
        """成功执行：Run.status=ok + step.done + 事件齐全。"""
        from bycall.agent.task_engine import execute_tool_loop_step
        from bycall.db import session_scope
        from bycall.governance.models import Run, Step, User

        uid = _create_user("alice")
        _tid, sid = _create_task_step(uid)
        bus = TaskEventBus()
        events = []
        bus.subscribe(None, events.append)

        with session_scope() as s:
            user = s.get(User, uid)
            step = s.get(Step, sid)
            run = execute_tool_loop_step(
                session=s,
                user=user,
                step=step,
                goal="查上周销量",
                chat_model=_FakeChatModel("上周销量 1234"),
                bus=bus,
            )
            assert run.status == "ok"
            assert "1234" in run.output_json
            assert run.tool_calls_json == "[]"
            assert run.finished_at is not None
            assert run.duration_ms >= 0

        with session_scope() as s:
            assert s.get(Step, sid).status == "done"
            runs = list(s.exec(Run.select_by_step(sid)))
            assert len(runs) == 1
            assert runs[0].status == "ok"

        types = [e.type for e in events]
        assert "step_started" in types
        assert "step_completed" in types

    def test_permission_denied(self, engine):
        """用户禁用 → Run.status=error + step.failed + step_failed（权限重算生效）。"""
        from bycall.agent.task_engine import execute_tool_loop_step
        from bycall.db import session_scope
        from bycall.governance.models import Step, User

        uid = _create_user("bob", is_active=False)
        _tid, sid = _create_task_step(uid)
        bus = TaskEventBus()
        failed = []
        bus.subscribe("step_failed", failed.append)

        with session_scope() as s:
            user = s.get(User, uid)
            step = s.get(Step, sid)
            run = execute_tool_loop_step(
                session=s,
                user=user,
                step=step,
                goal="x",
                chat_model=_FakeChatModel(),
                bus=bus,
            )
            assert run.status == "error"
            assert "禁用" in run.output_json

        with session_scope() as s:
            assert s.get(Step, sid).status == "failed"

        assert len(failed) == 1
        assert failed[0].payload["reason"] == "permission_denied"

    def test_execution_error(self, engine):
        """run_tool_loop 抛错 → Run.status=error + step.failed + step_failed。"""
        from bycall.agent.task_engine import execute_tool_loop_step
        from bycall.db import session_scope
        from bycall.governance.models import Step, User

        uid = _create_user("carol")
        _tid, sid = _create_task_step(uid)
        bus = TaskEventBus()
        failed = []
        bus.subscribe("step_failed", failed.append)

        with session_scope() as s:
            user = s.get(User, uid)
            step = s.get(Step, sid)
            run = execute_tool_loop_step(
                session=s,
                user=user,
                step=step,
                goal="x",
                chat_model=_BoomChatModel(),
                bus=bus,
            )
            assert run.status == "error"
            assert "LLM 挂了" in run.output_json

        with session_scope() as s:
            assert s.get(Step, sid).status == "failed"

        assert len(failed) == 1
        assert failed[0].payload["reason"] == "execution_error"
