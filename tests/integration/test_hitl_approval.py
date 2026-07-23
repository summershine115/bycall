"""HITL 审批队列 admin API 集成测试（M4 Slice C）。

端到端验证审批闭环：
- 列出 pending 审批（按状态过滤）
- approve → 执行工具 + 记录结果 + 状态变 executed
- reject → 状态变 rejected
- 重复审批 → 409
- 不存在的审批 → 404
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.tools.base import ToolSpec
from bycall.tools.registry import register


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed_tool_and_approval(*, tool_name="danger_delete", status="pending"):
    """注册一个测试工具 + 创建用户 + 创建审批请求，返回审批 id。"""
    register(
        ToolSpec(
            name=tool_name,
            description="危险操作",
            parameters={"type": "object", "properties": {"id": {"type": "string"}}},
            func=lambda **kw: f"deleted:{kw.get('id')}",
        )
    )
    with session_scope() as s:
        from bycall.governance.models import ApprovalRequest, User

        user = User(name="alice", display_name="Alice", role="admin")
        s.add(user)
        s.flush()
        req = ApprovalRequest(
            user_id=user.id,
            user_name="alice",
            conversation_id=None,
            channel="api",
            tool_name=tool_name,
            tool_args_json='{"id": "rec_42"}',
            status=status,
        )
        s.add(req)
        s.flush()
        rid = req.id
    return rid


class TestListApprovals:
    def test_list_all_and_filter_by_status(self, client):
        _seed_tool_and_approval()
        # 默认列出全部。
        resp = client.get("/api/admin/approvals")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["status"] == "pending"
        assert body[0]["tool_name"] == "danger_delete"
        assert body[0]["tool_args"] == {"id": "rec_42"}

        # 按状态过滤。
        resp = client.get("/api/admin/approvals?status=pending")
        assert len(resp.json()) == 1
        resp = client.get("/api/admin/approvals?status=executed")
        assert len(resp.json()) == 0

    def test_get_detail(self, client):
        rid = _seed_tool_and_approval()
        resp = client.get(f"/api/admin/approvals/{rid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == rid

    def test_get_not_found(self, client):
        resp = client.get("/api/admin/approvals/99999")
        assert resp.status_code == 404


class TestApprove:
    def test_approve_executes_and_records(self, client):
        """approve → 执行工具 + result_text 有值 + 状态 executed。"""
        rid = _seed_tool_and_approval()
        resp = client.post(f"/api/admin/approvals/{rid}/approve", json={"note": "ok"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "executed"
        assert "deleted:rec_42" in body["result_text"]
        assert body["review_note"] == "ok"
        assert body["reviewed_by"] != ""
        assert body["executed_at"] is not None

    def test_approve_not_found(self, client):
        resp = client.post("/api/admin/approvals/99999/approve", json={})
        assert resp.status_code == 404

    def test_approve_already_processed_returns_409(self, client):
        rid = _seed_tool_and_approval()
        # 第一次 approve 成功。
        resp = client.post(f"/api/admin/approvals/{rid}/approve", json={})
        assert resp.status_code == 200
        # 第二次 → 409。
        resp = client.post(f"/api/admin/approvals/{rid}/approve", json={})
        assert resp.status_code == 409

    def test_approve_tool_removed_marks_failed(self, client):
        """工具已被禁用/移除 → approve 标记 failed（不崩）。"""
        from bycall.governance.models import ApprovalRequest, User

        # 创建审批请求但不注册 gone_tool（模拟工具已被移除）。
        with session_scope() as s:
            user = User(name="ghost", display_name="Ghost", role="admin")
            s.add(user)
            s.flush()
            req = ApprovalRequest(
                user_id=user.id,
                user_name="ghost",
                tool_name="gone_tool",
                tool_args_json="{}",
                status="pending",
            )
            s.add(req)
            s.flush()
            rid = req.id
        # gone_tool 未注册 → execute 找不到。
        resp = client.post(f"/api/admin/approvals/{rid}/approve", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "不再可用" in body["result_text"]


class TestReject:
    def test_reject_marks_rejected(self, client):
        rid = _seed_tool_and_approval()
        resp = client.post(f"/api/admin/approvals/{rid}/reject", json={"note": "不允许"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["review_note"] == "不允许"

    def test_reject_already_processed_returns_409(self, client):
        rid = _seed_tool_and_approval()
        client.post(f"/api/admin/approvals/{rid}/reject", json={})
        resp = client.post(f"/api/admin/approvals/{rid}/reject", json={})
        assert resp.status_code == 409
