"""对话任务触发测试（S1a T5）。

验证：显式「启动任务：…」→ 创建 Task 走引擎（decision=task）；
普通问答 → 原 run_agent 路径（零回归，无 Task 创建）；任务触发落审计。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.config import get_settings


class _FakeLLM:
    """假 LLM：invoke 返回固定 AIMessage（run_tool_loop 空 tools 路径单轮结束）。"""

    def __init__(self, answer: str = "任务结果：销量 1234"):
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


def _user(name: str, role: str = "member") -> int:
    from bycall.db import session_scope
    from bycall.governance.models import User
    from bycall.governance.security import hash_password

    with session_scope() as s:
        u = User(name=name, display_name=name, role=role, password_hash=hash_password("x"))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


class TestTaskTrigger:
    def test_trigger_creates_task(self, engine):
        """显式「启动任务：…」→ 创建 Task + 走引擎 → decision=task。"""
        from bycall.agent.runner import run_agent
        from bycall.db import session_scope
        from bycall.governance.models import Task, User

        uid = _user("alice")
        with session_scope() as s:
            user = s.get(User, uid)
            result = run_agent(
                session=s,
                user=user,
                question="启动任务：查上周销量",
                llm=_FakeLLM(),
                channel="api",
            )
            assert result.decision == "task"
            assert "1234" in result.answer
            tasks = list(s.exec(Task.select_by_user(uid)))
            assert len(tasks) == 1
            assert tasks[0].trigger == "conversation"
            assert tasks[0].goal == "查上周销量"
            assert tasks[0].status == "completed"

    def test_normal_question_zero_regression(self, engine):
        """普通问答 → 原 run_agent 路径（decision 非 task，无 Task 创建）——零回归。"""
        from bycall.agent.runner import run_agent
        from bycall.db import session_scope
        from bycall.governance.models import Task, User

        uid = _user("bob")
        with session_scope() as s:
            user = s.get(User, uid)
            result = run_agent(session=s, user=user, question="你好", llm=_FakeLLM(), channel="api")
            # member 无 knowledge_scope → denied_no_access（原路径），但绝不是 task
            assert result.decision != "task"
            assert len(list(s.exec(Task.select_by_user(uid)))) == 0

    def test_trigger_without_goal_ignored(self, engine):
        """「启动任务：」（空 goal）→ 不触发，走原路径（避免空任务）。"""
        from bycall.agent.runner import run_agent
        from bycall.db import session_scope
        from bycall.governance.models import Task, User

        uid = _user("dave")
        with session_scope() as s:
            user = s.get(User, uid)
            result = run_agent(
                session=s, user=user, question="启动任务：", llm=_FakeLLM(), channel="api"
            )
            assert result.decision != "task"
            assert len(list(s.exec(Task.select_by_user(uid)))) == 0

    def test_task_audit_recorded(self, engine):
        """任务触发 → 审计落库（decision=task）。"""
        from bycall.agent.runner import run_agent
        from bycall.db import session_scope
        from bycall.governance.models import AuditLog, User

        uid = _user("carol")
        with session_scope() as s:
            user = s.get(User, uid)
            run_agent(
                session=s,
                user=user,
                question="启动任务：建工单",
                llm=_FakeLLM(),
                channel="api",
            )
            logs = list(s.exec(AuditLog.select_recent(10)))
            task_logs = [log for log in logs if log.decision == "task"]
            assert len(task_logs) >= 1
