"""控制面审计测试（P1-6：管理操作无审计记录）。

验证 create_user / delete_user / create_role / update_settings / delete_document
等管理变更操作写 AuditLog（channel='admin'，记录操作者 + 动作 + 变更前后值）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.governance.models import AuditLog


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """挂载 admin router + 完整 app（含会话守卫）的 TestClient。"""
    db_path = tmp_path / "test_audit.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    # 用完整 create_app（含会话守卫，这样 _audit_admin 能从 cookie 解析操作者）
    from bycall.api.app import create_app

    app = create_app()
    return TestClient(app)


def _login(c: TestClient):
    c.post("/api/login", data={"name": "admin", "password": "admin123"})


def _count_audit_logs(db_path: str, action: str | None = None) -> int:
    """查 AuditLog 表里 channel='admin' 的条数（可选按 action 过滤）。"""
    settings = get_settings()
    engine = create_engine(settings.postgres.dsn, connect_args={"check_same_thread": False})
    with Session(engine) as s:
        if action:
            return len(
                s.query(AuditLog)
                .filter(AuditLog.channel == "admin", AuditLog.action == action)
                .all()
            )
        return len(s.query(AuditLog).filter(AuditLog.channel == "admin").all())


def test_create_user_audited(client: TestClient):
    """create_user → AuditLog 多一行 action='create_user' channel='admin'。"""
    _login(client)
    before = _count_audit_logs(client.app.__dict__.get("_db_path", ""), "create_user")

    resp = client.post("/api/admin/users", json={"name": "alice", "role": "member"})
    assert resp.status_code == 201

    after = _count_audit_logs(None, "create_user")
    assert after == before + 1


def test_create_user_audit_records_operator(client: TestClient):
    """审计记录操作者 = 登录的 admin（user_name='admin'）。"""
    _login(client)
    client.post("/api/admin/users", json={"name": "bob", "role": "member"})

    settings = get_settings()
    engine = create_engine(settings.postgres.dsn, connect_args={"check_same_thread": False})
    with Session(engine) as s:
        log = (
            s.query(AuditLog)
            .filter(AuditLog.channel == "admin", AuditLog.action == "create_user")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.user_name == "admin"  # 操作者是登录的 admin
        assert "bob" in log.after  # after 快照含新用户名


def test_delete_user_audited(client: TestClient):
    """delete_user → 审计 action='delete_user'。"""
    _login(client)
    # 先建一个用户
    uid = client.post("/api/admin/users", json={"name": "charlie", "role": "member"}).json()["id"]
    before = _count_audit_logs(None, "delete_user")

    resp = client.delete(f"/api/admin/users/{uid}")
    assert resp.status_code == 204

    after = _count_audit_logs(None, "delete_user")
    assert after == before + 1


def test_create_role_audited(client: TestClient):
    """create_role → 审计 action='create_role'。"""
    _login(client)
    resp = client.post(
        "/api/admin/roles",
        json={
            "name": "sales",
            "description": "销售",
            "config": {"allowed_tools": [], "knowledge_scope_tags": ["sales"], "level": 0},
        },
    )
    assert resp.status_code == 201
    assert _count_audit_logs(None, "create_role") >= 1


def test_update_settings_audited(client: TestClient):
    """update_settings → 审计 action='update_settings'。"""
    _login(client)
    resp = client.put("/api/admin/settings", json={"settings": {"im.enable": "true"}})
    assert resp.status_code == 200
    assert _count_audit_logs(None, "update_settings") >= 1


def test_audit_masks_sensitive_fields(client: TestClient):
    """审计的 before/after 快照对敏感字段脱敏（如 password_hash）。"""
    _login(client)
    client.post(
        "/api/admin/users",
        json={"name": "sensitive_user", "role": "member", "password": "secret123"},
    )

    settings = get_settings()
    engine = create_engine(settings.postgres.dsn, connect_args={"check_same_thread": False})
    with Session(engine) as s:
        log = (
            s.query(AuditLog)
            .filter(AuditLog.action == "create_user")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert log is not None
        # password_hash 被脱敏，不是明文
        assert "secret123" not in log.after
        assert "masked" in log.after.lower() or "<masked>" in log.after
