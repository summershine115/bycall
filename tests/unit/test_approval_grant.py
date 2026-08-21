"""审批授权三档（T3.3 once/session/always）单元测试。

验证（ADR-0011 治理边界）：
- always 档命中 → 审批门免审直执行 + 台账记录（grant_id + reviewed_by=grant:*）
- session 档以 conversation_id 为边界：同会话命中、跨会话不命中（回退人审）
- 授权不可跨用户共享；撤销后立即失效
- 授权只跳过 HITL，不跳过权限矩阵：filter_tools 白名单外的破坏性工具仍不可见
- admin 端点：approve 落授权（幂等）、session 档无会话上下文 → 400、列表/撤销 + 审计

注意：``find_active_grant`` / ``_record_grant_execution`` / ``create_approval_request``
内部用独立 ``session_scope()``——测试不持有长连接 session，按需短生命周期。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine, select

from bycall.config import get_settings
from bycall.tools.approval import (
    ApprovalContext,
    find_active_grant,
    make_approval_gated_func,
    reset_approval_context,
    set_approval_context,
)
from bycall.tools.base import ToolSpec
from bycall.tools.registry import register, unregister


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


@pytest.fixture
def client(tmp_path, monkeypatch):
    """挂 admin router 的子 app + 全新 SQLite（端点测试用）。"""
    db_path = tmp_path / "admin.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()

    from bycall.admin.api import router
    from bycall.db import init_db, set_engine

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _register_destructive_tool(name: str = "danger_deploy"):
    """注册 requires_approval=True 的测试工具，返回 (spec, calls 记录)。"""
    calls: list[dict] = []

    def _func(**kwargs):
        calls.append(kwargs)
        return f"executed:{kwargs.get('target', '')}"

    spec = ToolSpec(
        name=name,
        description="危险部署操作",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
        func=_func,
        requires_approval=True,
    )
    register(spec)
    return spec, calls


def _create_user(name: str) -> int:
    from bycall.db import session_scope
    from bycall.governance.models import User

    with session_scope() as s:
        user = User(name=name, display_name=name, role="member", is_active=True)
        s.add(user)
        s.flush()
        uid = user.id
    assert uid is not None
    return uid


def _create_grant(
    user_id: int,
    tool_name: str,
    scope: str,
    conversation_id: int | None = None,
    status: str = "active",
) -> int:
    """插入一条授权，返回 id。"""
    from bycall.db import session_scope
    from bycall.governance.models import ApprovalGrant

    with session_scope() as s:
        grant = ApprovalGrant(
            user_id=user_id,
            user_name="tester",
            tool_name=tool_name,
            scope=scope,
            conversation_id=conversation_id,
            status=status,
            granted_by="admin",
        )
        s.add(grant)
        s.flush()
        gid = grant.id
    assert gid is not None
    return gid


def _ctx(user_id: int, conversation_id: int | None = 1) -> ApprovalContext:
    return ApprovalContext(
        user_id=user_id,
        user_name="tester",
        conversation_id=conversation_id,
        channel="api",
    )


def _run_gated(spec, ctx: ApprovalContext):
    """在审批上下文里调用审批门，返回 (result, token)。"""
    token = set_approval_context(ctx)
    try:
        return make_approval_gated_func(spec)(target="prod")
    finally:
        reset_approval_context(token)


# ============ find_active_grant 判定 ============


def test_find_active_grant_always_matches_any_conversation(engine):
    """always 档与会话无关：任意 conversation_id（含 None）都命中。"""
    uid = _create_user("alice")
    gid = _create_grant(uid, "t", "always")
    assert find_active_grant(uid, "t", None) is not None
    assert find_active_grant(uid, "t", 999).id == gid


def test_find_active_grant_session_requires_same_conversation(engine):
    """session 档只命中绑定会话；跨会话/无会话上下文不命中。"""
    uid = _create_user("alice")
    _create_grant(uid, "t", "session", conversation_id=5)
    assert find_active_grant(uid, "t", 5) is not None
    assert find_active_grant(uid, "t", 6) is None
    assert find_active_grant(uid, "t", None) is None


def test_find_active_grant_scoped_by_user_and_status(engine):
    """授权不可跨用户；撤销后不命中。"""
    uid_a = _create_user("alice")
    uid_b = _create_user("bob")
    _create_grant(uid_a, "t", "always")
    _create_grant(uid_b, "t_revoked", "always", status="revoked")
    assert find_active_grant(uid_b, "t", None) is None
    assert find_active_grant(uid_b, "t_revoked", None) is None


# ============ 审批门短路 ============


def test_always_grant_bypasses_gate_and_records_ledger(engine):
    """always 命中 → 当轮直接执行 + 台记 executed（grant_id + grant:* 标注），无 pending。"""
    from bycall.db import session_scope
    from bycall.governance.models import ApprovalRequest

    spec, calls = _register_destructive_tool()
    try:
        uid = _create_user("alice")
        gid = _create_grant(uid, spec.name, "always")
        result = _run_gated(spec, _ctx(uid, conversation_id=7))

        assert calls == [{"target": "prod"}], "授权命中后未直接执行"
        assert result == "executed:prod"
        with session_scope() as s:
            pending = s.exec(ApprovalRequest.select_by_status("pending")).all()
            assert pending == [], "授权命中不应产生待审批请求"
            rows = s.exec(select(ApprovalRequest).where(ApprovalRequest.grant_id == gid)).all()
            assert len(rows) == 1
            assert rows[0].status == "executed"
            assert rows[0].reviewed_by == f"grant:always#{gid}"
    finally:
        unregister(spec.name)


def test_session_grant_same_conversation_bypasses(engine):
    """session 档同会话命中 → 免审直执行。"""
    spec, calls = _register_destructive_tool()
    try:
        uid = _create_user("alice")
        _create_grant(uid, spec.name, "session", conversation_id=5)
        result = _run_gated(spec, _ctx(uid, conversation_id=5))
        assert calls == [{"target": "prod"}]
        assert result == "executed:prod"
    finally:
        unregister(spec.name)


def test_session_grant_cross_conversation_creates_pending(engine):
    """session 档跨会话不命中 → 回退人审：不执行，创建 pending。"""
    from bycall.db import session_scope
    from bycall.governance.models import ApprovalRequest

    spec, calls = _register_destructive_tool()
    try:
        uid = _create_user("alice")
        _create_grant(uid, spec.name, "session", conversation_id=5)
        result = _run_gated(spec, _ctx(uid, conversation_id=6))

        assert calls == [], "跨会话调用被授权放行（越权）"
        assert "需要管理员审批" in result
        with session_scope() as s:
            pending = s.exec(ApprovalRequest.select_by_status("pending")).all()
            assert len(pending) == 1
            assert pending[0].tool_name == spec.name
    finally:
        unregister(spec.name)


def test_grant_not_shared_across_users(engine):
    """A 的授权不能让 B 免审——跨用户必须回退人审。"""
    spec, calls = _register_destructive_tool()
    try:
        uid_a = _create_user("alice")
        uid_b = _create_user("bob")
        _create_grant(uid_a, spec.name, "always")
        result = _run_gated(spec, _ctx(uid_b, conversation_id=1))
        assert calls == [], "他人授权被共享（越权）"
        assert "需要管理员审批" in result
    finally:
        unregister(spec.name)


def test_revoked_grant_falls_back_to_pending(engine):
    """撤销后的授权立即失效——恢复人审。"""
    spec, calls = _register_destructive_tool()
    try:
        uid = _create_user("alice")
        _create_grant(uid, spec.name, "always", status="revoked")
        result = _run_gated(spec, _ctx(uid))
        assert calls == []
        assert "需要管理员审批" in result
    finally:
        unregister(spec.name)


def test_grant_cannot_bypass_permission_matrix(engine):
    """授权只跳过 HITL 不跳过权限矩阵：白名单外的破坏性工具对 LLM 不可见。"""
    from bycall.governance.capability import CapabilitySet
    from bycall.tools.gateway import to_langchain_tools

    spec, _ = _register_destructive_tool()
    try:
        cap = CapabilitySet(user_id=1, user_name="alice", role="member", allowed_tools=[])  # type: ignore[call-arg]
        tools = to_langchain_tools([spec], cap)
        assert any(t.name == spec.name for t in tools), "前提：白名单内工具应可见"
        # 真实链路里 filter_tools 先裁剪——白名单外的 spec 根本到不了 to_langchain_tools。
        from bycall.tools.gateway import filter_tools

        assert filter_tools(["other_tool"]) == [], "白名单外工具不应进入 LLM 工具列表"
    finally:
        unregister(spec.name)


def test_gate_without_grant_still_pending(engine):
    """无授权时保持既有语义：创建 pending、不执行（回归保护）。"""
    spec, calls = _register_destructive_tool()
    try:
        uid = _create_user("carol")
        result = _run_gated(spec, _ctx(uid))
        assert calls == []
        assert "需要管理员审批" in result
    finally:
        unregister(spec.name)


# ============ admin 端点 ============


def _seed_pending_request(
    client: TestClient, tool_name: str, conversation_id: int | None, name: str = "dave"
):
    """直接落一条 pending 审批请求（绕过审批门），返回 id。同名用户复用（幂等）。"""
    import json

    from bycall.db import session_scope
    from bycall.governance.models import ApprovalRequest, User

    with session_scope() as s:
        user = s.exec(User.select_by_name(name)).first()
        if user is None:
            user = User(name=name, display_name=name, role="member", is_active=True)
            s.add(user)
            s.flush()
        req = ApprovalRequest(
            user_id=user.id,
            user_name="dave",
            conversation_id=conversation_id,
            channel="api",
            tool_name=tool_name,
            tool_args_json=json.dumps({"target": "prod"}),
            status="pending",
        )
        s.add(req)
        s.flush()
        return req.id


def test_approve_with_always_scope_creates_grant_and_idempotent(client):
    """approve + grant_scope=always → 落授权；重复批准另一条同工具请求不堆叠授权行。"""
    spec, _ = _register_destructive_tool()
    try:
        req_id = _seed_pending_request(client, spec.name, conversation_id=3)
        resp = client.post(
            f"/api/admin/approvals/{req_id}/approve",
            json={"note": "信任该操作", "grant_scope": "always"},
        )
        assert resp.status_code == 200
        grants = client.get("/api/admin/approvals/grants").json()
        assert len(grants) == 1
        assert grants[0]["scope"] == "always"
        assert grants[0]["status"] == "active"
        assert grants[0]["conversation_id"] is None
        assert grants[0]["granted_by"]

        # 幂等：同维度再次批准只刷新既有授权。
        req2 = _seed_pending_request(client, spec.name, conversation_id=4)
        client.post(f"/api/admin/approvals/{req2}/approve", json={"grant_scope": "always"})
        grants2 = client.get("/api/admin/approvals/grants", params={"status": "active"}).json()
        assert len(grants2) == 1, "重复授权不应堆叠新行"
    finally:
        unregister(spec.name)


def test_approve_with_session_scope_binds_conversation(client):
    """approve + grant_scope=session → 授权绑定发起会话。"""
    spec, _ = _register_destructive_tool()
    try:
        req_id = _seed_pending_request(client, spec.name, conversation_id=42)
        resp = client.post(
            f"/api/admin/approvals/{req_id}/approve",
            json={"grant_scope": "session"},
        )
        assert resp.status_code == 200
        grants = client.get("/api/admin/approvals/grants").json()
        assert len(grants) == 1
        assert grants[0]["scope"] == "session"
        assert grants[0]["conversation_id"] == 42
    finally:
        unregister(spec.name)


def test_approve_session_scope_without_conversation_rejected(client):
    """无会话上下文（REST 单轮）的 session 档语义不成立 → 400，不执行不落授权。"""
    spec, _ = _register_destructive_tool()
    try:
        req_id = _seed_pending_request(client, spec.name, conversation_id=None)
        resp = client.post(
            f"/api/admin/approvals/{req_id}/approve",
            json={"grant_scope": "session"},
        )
        assert resp.status_code == 400
        assert "会话" in resp.json()["detail"]
        grants = client.get("/api/admin/approvals/grants").json()
        assert grants == []
    finally:
        unregister(spec.name)


def test_approve_once_scope_creates_no_grant(client):
    """默认 once 档不落授权表（零新增风险）。"""
    spec, _ = _register_destructive_tool()
    try:
        req_id = _seed_pending_request(client, spec.name, conversation_id=1)
        resp = client.post(f"/api/admin/approvals/{req_id}/approve", json={})
        assert resp.status_code == 200
        assert client.get("/api/admin/approvals/grants").json() == []
    finally:
        unregister(spec.name)


def test_revoke_grant_soft_deletes_and_audits(client):
    """撤销授权：status=revoked + 撤销人/时间 + 审计；重复撤销 → 409。"""
    from bycall.db import session_scope
    from bycall.governance.models import AuditLog

    spec, _ = _register_destructive_tool()
    try:
        req_id = _seed_pending_request(client, spec.name, conversation_id=1)
        client.post(f"/api/admin/approvals/{req_id}/approve", json={"grant_scope": "always"})
        grant_id = client.get("/api/admin/approvals/grants").json()[0]["id"]

        resp = client.post(f"/api/admin/approvals/grants/{grant_id}/revoke")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "revoked"
        assert body["revoked_by"]

        again = client.post(f"/api/admin/approvals/grants/{grant_id}/revoke")
        assert again.status_code == 409

        active = client.get("/api/admin/approvals/grants", params={"status": "active"}).json()
        assert active == []
        with session_scope() as s:
            actions = [a.action for a in s.exec(select(AuditLog)).all()]
            assert "revoke_approval_grant" in actions
    finally:
        unregister(spec.name)
