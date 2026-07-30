"""Task/Step/Run 持久化多步任务模型测试（S1a 执行核心 T1）。

验证三实体 CRUD + 关联查询 + 默认值。复用 test_approval 的临时 sqlite fixture 模式
（init_db → SQLModel.metadata.create_all 自动建新表）。
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db。不持有 session——测试按需用 session_scope()。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()


# ============ 辅助：短 session 插入 ============


def _create_task(user_id: int, goal: str = "帮我查上周销量", **overrides) -> int:
    """在短 session 里创建一条 Task，返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import Task

    with session_scope() as s:
        task = Task(user_id=user_id, goal=goal, **overrides)
        s.add(task)
        s.commit()
        s.refresh(task)
        return task.id


def _create_step(task_id: int, step_index: int, kind: str = "tool_loop", **overrides) -> int:
    """在短 session 里创建一条 Step，返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import Step

    with session_scope() as s:
        step = Step(task_id=task_id, step_index=step_index, kind=kind, **overrides)
        s.add(step)
        s.commit()
        s.refresh(step)
        return step.id


def _create_run(step_id: int, status: str = "ok", **overrides) -> int:
    """在短 session 里创建一条 Run，返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import Run

    with session_scope() as s:
        run = Run(step_id=step_id, status=status, **overrides)
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


# ============ Task ============


class TestTaskModel:
    def test_create_defaults(self, engine):
        """新建 Task 取全部默认值。"""
        from bycall.db import session_scope
        from bycall.governance.models import Task

        tid = _create_task(1)
        with session_scope() as s:
            t = s.get(Task, tid)
            assert t.user_id == 1
            assert t.status == "draft"
            assert t.trigger == "conversation"
            assert t.schedule_id is None
            assert t.conversation_id is None
            assert t.channel == ""
            assert t.plan_json == "[]"
            assert t.capability_snapshot_json == "{}"
            assert t.created_at is not None
            assert t.updated_at is not None

    def test_select_by_user(self, engine):
        """按 user_id 列任务，只返回该用户的，按创建倒序。"""
        from bycall.db import session_scope
        from bycall.governance.models import Task

        _create_task(1, goal="A")
        _create_task(1, goal="B")
        _create_task(2, goal="C")
        with session_scope() as s:
            rows = list(s.exec(Task.select_by_user(1)))
            assert [r.goal for r in rows] == ["B", "A"]  # 倒序
            assert all(r.user_id == 1 for r in rows)

    def test_select_by_status(self, engine):
        """按状态列任务。"""
        from bycall.db import session_scope
        from bycall.governance.models import Task

        _create_task(1, status="running")
        _create_task(1, status="paused")
        _create_task(1, status="running")
        with session_scope() as s:
            running = list(s.exec(Task.select_by_status("running")))
            assert len(running) == 2
            assert all(r.status == "running" for r in running)

    def test_select_by_conversation(self, engine):
        """按会话查任务（S1a 1:1，最新在前）。"""
        from bycall.db import session_scope
        from bycall.governance.models import Task

        _create_task(1, conversation_id=10, goal="旧")
        _create_task(1, conversation_id=10, goal="新")
        _create_task(1, conversation_id=11, goal="别")
        with session_scope() as s:
            rows = list(s.exec(Task.select_by_conversation(10)))
            assert [r.goal for r in rows] == ["新", "旧"]

    def test_trigger_and_schedule(self, engine):
        """cron/event 触发的任务带 schedule_id。"""
        from bycall.db import session_scope
        from bycall.governance.models import Task

        tid = _create_task(1, trigger="cron", schedule_id=5, channel="im")
        with session_scope() as s:
            t = s.get(Task, tid)
            assert t.trigger == "cron"
            assert t.schedule_id == 5
            assert t.channel == "im"


# ============ Step ============


class TestStepModel:
    def test_create_defaults(self, engine):
        """新建 Step 默认 pending + tool_loop。"""
        from bycall.db import session_scope
        from bycall.governance.models import Step

        tid = _create_task(1)
        sid = _create_step(tid, 0)
        with session_scope() as s:
            st = s.get(Step, sid)
            assert st.task_id == tid
            assert st.step_index == 0
            assert st.kind == "tool_loop"
            assert st.status == "pending"
            assert st.spec_json == "{}"
            assert st.retry_count == 0
            assert st.max_retries == 0

    def test_select_by_task_ordered(self, engine):
        """按 task_id 列 Step，step_index 正序。"""
        from bycall.db import session_scope
        from bycall.governance.models import Step

        tid = _create_task(1)
        _create_step(tid, 2, kind="clarify")
        _create_step(tid, 0, kind="tool_loop")
        _create_step(tid, 1, kind="approval_wait")
        with session_scope() as s:
            rows = list(s.exec(Step.select_by_task(tid)))
            assert [r.step_index for r in rows] == [0, 1, 2]
            assert [r.kind for r in rows] == ["tool_loop", "approval_wait", "clarify"]

    def test_idempotency_key(self, engine):
        """幂等键可显式设置，供恢复时去重。"""
        from bycall.db import session_scope
        from bycall.governance.models import Step

        tid = _create_task(1)
        sid = _create_step(tid, 0, idempotency_key="task-1-step-0", status="done")
        with session_scope() as s:
            st = s.get(Step, sid)
            assert st.idempotency_key == "task-1-step-0"
            assert st.status == "done"


# ============ Run ============


class TestRunModel:
    def test_create_defaults(self, engine):
        """新建 Run 默认 ok + started_at 已填。"""
        from bycall.db import session_scope
        from bycall.governance.models import Run

        tid = _create_task(1)
        sid = _create_step(tid, 0)
        rid = _create_run(sid)
        with session_scope() as s:
            r = s.get(Run, rid)
            assert r.step_id == sid
            assert r.status == "ok"
            assert r.duration_ms == 0
            assert r.tool_calls_json == "[]"
            assert r.input_json == "{}"
            assert r.output_json == "{}"
            assert r.started_at is not None
            assert r.finished_at is None  # 未填

    def test_select_by_step(self, engine):
        """按 step_id 列 Run（重试场景：多条 Run）。"""
        from bycall.db import session_scope
        from bycall.governance.models import Run

        tid = _create_task(1)
        sid = _create_step(tid, 0)
        _create_run(sid, status="error")
        _create_run(sid, status="ok")
        with session_scope() as s:
            rows = list(s.exec(Run.select_by_step(sid)))
            assert [r.status for r in rows] == ["error", "ok"]


# ============ 关联 Task → Step → Run ============


class TestTaskGraph:
    def test_full_chain(self, engine):
        """Task → 多个 Step → 每个 Step 的 Run 都可查回。"""
        from bycall.db import session_scope
        from bycall.governance.models import Run, Step

        tid = _create_task(1, goal="三步任务")
        s0 = _create_step(tid, 0, kind="tool_loop")
        s1 = _create_step(tid, 1, kind="approval_wait")
        s2 = _create_step(tid, 2, kind="tool_loop")
        _create_run(s0, status="ok")
        _create_run(s1, status="ok")
        _create_run(s2, status="error")

        with session_scope() as s:
            steps = list(s.exec(Step.select_by_task(tid)))
            assert len(steps) == 3
            # 每个 step 都有恰好一条 run
            for st in steps:
                runs = list(s.exec(Run.select_by_step(st.id)))
                assert len(runs) == 1
