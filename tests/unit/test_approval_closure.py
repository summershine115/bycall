"""任务审批自动闭环测试（S1b）。

覆盖：approval_wait 步创建审批请求 + 任务暂停；审批通过 → resume → 步完成 →
任务完成；审批拒绝 → resume → 步失败 → 任务失败；requires_approval 工具被调用 →
Step 转 awaiting_approval + 暂停。
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
        u = User(name=name, display_name=name, role="member", password_hash=hash_password("x"))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id  # type: ignore[return-value]


def _approve(session, req) -> None:
    """模拟 admin 审批通过（execute_approved_tool 语义，任务级伪工具直接通过）。"""
    from bycall.tools.approval import execute_approved_tool

    ok, result = execute_approved_tool(req)
    assert ok
    req.status = "executed"
    req.result_text = result
    session.add(req)
    session.commit()


def _reject(session, req) -> None:
    """模拟 admin 拒绝审批。"""
    req.status = "rejected"
    session.add(req)
    session.commit()


class TestApprovalWaitClosure:
    def test_approval_wait_pauses_and_resumes(self, engine):
        """approval_wait 步：创建审批 + 暂停；审批通过 → resume → 完成。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import ApprovalRequest, Step, Task

        uid = _user()
        with session_scope() as s:
            t = Task(
                user_id=uid,
                channel="api",
                trigger="conversation",
                goal="需要审批的任务",
            )
            s.add(t)
            s.commit()
            s.refresh(t)

            # 两段式：tool_loop 步 + approval_wait 步。
            run_task(
                session=s,
                task=t,
                chat_model=_FakeChatModel(),
                plan=[
                    {"goal": "第一步", "kind": "tool_loop"},
                    {"goal": "人工确认", "kind": "approval_wait"},
                ],
            )
            assert t.status == "paused"
            steps = list(s.exec(Step.select_by_task(t.id)))
            assert steps[1].status == "awaiting_approval"
            req = s.exec(ApprovalRequest.select_by_step(steps[1].id)).first()
            assert req is not None
            assert req.tool_name == "__task_approval__"
            assert req.task_id == t.id

            # 审批通过 → resume → 步 done → 任务完成。
            _approve(s, req)
            run_task(session=s, task=t, chat_model=_FakeChatModel())
            assert t.status == "completed"
            steps = list(s.exec(Step.select_by_task(t.id)))
            assert all(st.status == "done" for st in steps)

    def test_approval_rejected_fails_task(self, engine):
        """审批拒绝 → resume → 对应步失败 → 任务失败。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import ApprovalRequest, Step, Task

        uid = _user()
        with session_scope() as s:
            t = Task(user_id=uid, channel="api", trigger="conversation", goal="审批任务")
            s.add(t)
            s.commit()
            s.refresh(t)
            run_task(
                session=s,
                task=t,
                chat_model=_FakeChatModel(),
                plan=[
                    {"goal": "第一步", "kind": "tool_loop"},
                    {"goal": "人工确认", "kind": "approval_wait"},
                ],
            )
            assert t.status == "paused"
            steps = list(s.exec(Step.select_by_task(t.id)))
            req = s.exec(ApprovalRequest.select_by_step(steps[1].id)).first()

            _reject(s, req)
            run_task(session=s, task=t, chat_model=_FakeChatModel())
            assert t.status == "failed"
            steps = list(s.exec(Step.select_by_task(t.id)))
            assert steps[0].status == "done"
            assert steps[1].status == "failed"

    def test_still_pending_stays_paused(self, engine):
        """审批仍 pending → resume 保持暂停（不推进不失败）。"""
        from bycall.agent.task_engine import run_task
        from bycall.db import session_scope
        from bycall.governance.models import Step, Task

        uid = _user()
        with session_scope() as s:
            t = Task(user_id=uid, channel="api", trigger="conversation", goal="审批任务")
            s.add(t)
            s.commit()
            s.refresh(t)
            run_task(
                session=s,
                task=t,
                chat_model=_FakeChatModel(),
                plan=[{"goal": "人工确认", "kind": "approval_wait"}],
            )
            assert t.status == "paused"
            run_task(session=s, task=t, chat_model=_FakeChatModel())
            assert t.status == "paused"
            steps = list(s.exec(Step.select_by_task(t.id)))
            assert steps[0].status == "awaiting_approval"


class TestGatedToolInterception:
    def _setup_gated_tool(self):
        """注册一个 requires_approval 测试工具，返回工具名。"""
        from bycall.tools.base import ToolSpec
        from bycall.tools.registry import register

        name = "gated_destructive_tool"
        register(
            ToolSpec(
                name=name,
                description="破坏性测试工具",
                parameters={"type": "object", "properties": {}},
                func=lambda **kw: "已执行",
                requires_approval=True,
            )
        )
        return name

    def _grant_tool(self, user_id: int, tool_name: str) -> None:
        """给用户加工具权限（member 角色白名单）。

        PydanticJSONType 列无 mutation tracking：必须整体重新赋值才会持久化。
        """
        from bycall.db import session_scope
        from bycall.governance.models import Role

        with session_scope() as s:
            role = s.exec(Role.select_by_name("member")).first()
            role.config = role.config.model_copy(
                update={"allowed_tools": [*role.config.allowed_tools, tool_name]}
            )
            s.add(role)
            s.commit()

    def test_gated_tool_intercepts_step(self, engine, monkeypatch):
        """Step 内调用 requires_approval 工具 → 审批门拦截 → Step 等待审批 + 任务暂停。"""
        import json

        from bycall.agent.task_engine import execute_tool_loop_step, run_task
        from bycall.db import session_scope
        from bycall.governance.models import ApprovalRequest, Run, Step, Task, User

        tool = self._setup_gated_tool()
        uid = _user()
        self._grant_tool(uid, tool)

        # 打桩 run_tool_loop：模拟审批门行为——调用被拦截工具时创建审批请求
        # （读 execute_tool_loop_step 已设置的审批上下文，带 task_id/step_id 关联），
        # 然后返回含被拦截工具的 tool_calls。
        def fake_run_tool_loop(**kwargs):
            from bycall.tools.approval import create_approval_request, get_approval_context

            ctx = get_approval_context()
            assert ctx is not None and ctx.task_id is not None and ctx.step_id is not None
            create_approval_request(ctx, tool, {"target": "x"})

            class _G:
                def __init__(self):
                    self.answer = "已提交审批"
                    self.hit_iteration_limit = False
                    self.tool_calls_made = [
                        {"name": tool, "args": {"target": "x"}, "result": "需审批"}
                    ]

            return _G()

        monkeypatch.setattr("bycall.agent.graph.run_tool_loop", fake_run_tool_loop)

        with session_scope() as s:
            t = Task(user_id=uid, channel="api", trigger="conversation", goal="拦截任务")
            s.add(t)
            s.commit()
            s.refresh(t)
            step = Step(task_id=t.id, step_index=0, kind="tool_loop", spec_json='{"goal": "x"}')
            s.add(step)
            s.commit()
            s.refresh(step)

            execute_tool_loop_step(
                session=s,
                user=s.get(User, uid),
                step=step,
                goal="x",
                chat_model=_FakeChatModel(),
            )

            assert step.status == "awaiting_approval"
            req = s.exec(ApprovalRequest.select_by_step(step.id)).first()
            assert req is not None and req.status == "pending"
            runs = list(s.exec(Run.select_by_step(step.id)))
            assert len(runs) == 1
            assert json.loads(runs[0].tool_calls_json)[0]["name"] == tool

            # 审批通过 → run_task resume 推进（awaiting_approval → done → completed）。
            _approve(s, req)
            run_task(session=s, task=t, chat_model=_FakeChatModel())
            assert t.status == "completed"
            assert step.status == "done"
