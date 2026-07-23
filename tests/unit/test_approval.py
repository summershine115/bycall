"""HITL 审批门单元测试（M4 Slice C）。

验证：
- 审批门拦截破坏性工具调用：创建 ApprovalRequest + 返回"待审批"消息（不执行工具）
- 无审批上下文时降级为直接执行
- ApprovalRequest 模型查询
- execute_approved_tool 的权限重验 + 执行 + 失败路径

注意：``create_approval_request`` / ``execute_approved_tool`` 内部用独立 ``session_scope()``，
故测试不能持有长连接 session（SQLite 单写锁会死锁）。用短生命周期 session：写完即关。
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.tools.approval import (
    ApprovalContext,
    execute_approved_tool,
    get_approval_context,
    make_approval_gated_func,
    reset_approval_context,
    set_approval_context,
)
from bycall.tools.base import ToolSpec
from bycall.tools.registry import register


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db。不持有 session——测试按需用 session_scope()。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(
        create_engine(
            f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}
        )
    )
    init_db()


def _register_destructive_tool(name: str = "danger_delete"):
    """注册一个 requires_approval=True 的测试工具，返回 (spec, calls 记录)。"""
    calls: list[dict] = []

    def _func(**kwargs):
        calls.append(kwargs)
        return f"executed:{kwargs}"

    spec = ToolSpec(
        name=name,
        description="危险删除操作",
        parameters={"type": "object", "properties": {}},
        func=_func,
        requires_approval=True,
    )
    register(spec)
    return spec, calls


def _create_user(name: str, role: str = "member", is_active: bool = True) -> int:
    """在短 session 里插入用户，返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import User

    with session_scope() as s:
        user = User(name=name, display_name=name, role=role, is_active=is_active)
        s.add(user)
        s.flush()
        uid = user.id
    assert uid is not None
    return uid


# ============ 审批门 ============


class TestApprovalGate:
    """make_approval_gated_func 的拦截逻辑。"""

    def test_gate_creates_request_and_returns_pending(self, engine):
        """有审批上下文 → 创建 ApprovalRequest，返回"待审批"消息，工具不执行。"""
        from bycall.governance.models import ApprovalRequest

        spec, calls = _register_destructive_tool()
        uid = _create_user("alice")

        ctx = ApprovalContext(user_id=uid, user_name="alice", conversation_id=None, channel="api")
        token = set_approval_context(ctx)
        try:
            gated = make_approval_gated_func(spec)
            result = gated(target="record_42")
        finally:
            reset_approval_context(token)

        # 工具未被调用。
        assert calls == []
        # 返回"待审批"消息。
        assert "审批请求 #" in result
        assert "danger_delete" in result
        assert "record_42" in result

        # ApprovalRequest 已落库（pending）。
        from bycall.db import session_scope

        with session_scope() as s:
            reqs = s.exec(ApprovalRequest.select_all()).all()
            assert len(reqs) == 1
            assert reqs[0].status == "pending"
            assert reqs[0].tool_name == "danger_delete"
            assert reqs[0].user_name == "alice"
            assert "record_42" in reqs[0].tool_args_json

    def test_gate_degrades_without_context(self):
        """无审批上下文 → 降级为直接执行（不拦截）。"""
        spec, calls = _register_destructive_tool("direct_exec")
        assert get_approval_context() is None

        gated = make_approval_gated_func(spec)
        result = gated(x="hello")

        assert calls == [{"x": "hello"}]
        assert "executed:" in result


# ============ ApprovalRequest 模型查询 ============


class TestApprovalModelQueries:
    """ApprovalRequest 的 select_* 查询。"""

    def test_select_pending_and_by_status(self, engine):
        from bycall.db import session_scope
        from bycall.governance.models import ApprovalRequest

        with session_scope() as s:
            for i, status in enumerate(["pending", "pending", "executed", "rejected"]):
                s.add(
                    ApprovalRequest(
                        user_id=1,
                        user_name="tester",
                        tool_name=f"tool_{i}",
                        tool_args_json="{}",
                        status=status,
                    )
                )

        with session_scope() as s:
            pending = s.exec(ApprovalRequest.select_pending()).all()
            assert len(pending) == 2
            assert all(r.status == "pending" for r in pending)

            executed = s.exec(ApprovalRequest.select_by_status("executed")).all()
            assert len(executed) == 1

            all_reqs = s.exec(ApprovalRequest.select_all()).all()
            assert len(all_reqs) == 4


# ============ execute_approved_tool ============


def _create_approval(user_id: int, tool_name: str, args_json: str = "{}") -> int:
    """在短 session 里创建一条 pending 审批请求，返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import ApprovalRequest

    with session_scope() as s:
        req = ApprovalRequest(
            user_id=user_id,
            user_name="tester",
            tool_name=tool_name,
            tool_args_json=args_json,
            status="pending",
        )
        s.add(req)
        s.flush()
        rid = req.id
    assert rid is not None
    return rid


def _load_approval_detached(rid: int):
    """加载审批请求并从 session 分离（供 execute_approved_tool 读属性，不持锁）。"""
    from bycall.db import session_scope
    from bycall.governance.models import ApprovalRequest

    with session_scope() as s:
        req = s.get(ApprovalRequest, rid)
        s.expunge(req)
    return req


class TestExecuteApprovedTool:
    """execute_approved_tool 的执行 + 权限重验 + 失败路径。"""

    def test_execute_success(self, engine):
        """审批通过后执行工具 → (True, result)。admin 用户有通配白名单。"""
        def _delete(**kwargs):
            return f"deleted:{kwargs.get('id')}"

        register(
            ToolSpec(
                name="safe_delete",
                description="删除",
                parameters={"type": "object", "properties": {"id": {"type": "string"}}},
                func=_delete,
            )
        )
        uid = _create_user("admin_test", role="admin")
        rid = _create_approval(uid, "safe_delete", '{"id": "rec_99"}')

        req = _load_approval_detached(rid)
        ok, result = execute_approved_tool(req)
        assert ok is True
        assert "deleted:rec_99" in result

    def test_execute_tool_not_in_registry(self, engine):
        """工具不在注册表 → (False, "不再可用")。"""
        uid = _create_user("carol", role="admin")
        rid = _create_approval(uid, "nonexistent_tool")

        req = _load_approval_detached(rid)
        ok, result = execute_approved_tool(req)
        assert ok is False
        assert "不再可用" in result

    def test_execute_tool_not_in_whitelist(self, engine):
        """工具不在用户当前白名单 → (False, "权限")。"""
        from bycall.db import session_scope
        from bycall.governance.models import Role, RoleConfig

        def _noop(**kwargs):
            return "done"

        register(
            ToolSpec(
                name="restricted_op",
                description="受限操作",
                parameters={"type": "object", "properties": {}},
                func=_noop,
            )
        )
        with session_scope() as s:
            s.add(Role(name="limited", config=RoleConfig(allowed_tools=[], knowledge_scope_tags=["public"])))
        uid = _create_user("dave", role="limited")
        rid = _create_approval(uid, "restricted_op")

        req = _load_approval_detached(rid)
        ok, result = execute_approved_tool(req)
        assert ok is False
        assert "权限" in result

    def test_execute_user_disabled(self, engine):
        """用户已被禁用 → (False, "已禁用")。"""
        def _noop(**kwargs):
            return "done"

        register(
            ToolSpec(
                name="any_tool",
                description="任意",
                parameters={"type": "object", "properties": {}},
                func=_noop,
            )
        )
        uid = _create_user("disabled_user", role="admin", is_active=False)
        rid = _create_approval(uid, "any_tool")

        req = _load_approval_detached(rid)
        ok, result = execute_approved_tool(req)
        assert ok is False
        assert "禁用" in result
