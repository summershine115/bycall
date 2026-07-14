"""管理员实时撤权测试（P1-1：守卫只信 cookie 缓存）。

验证 admin 被禁用/改密/降级后，旧 cookie 立即失效（每请求重查 DB）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router as admin_router
from bycall.config import get_settings
from bycall.db import init_db, set_engine

SESSION_COOKIE = "bycall_session"


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    """挂载完整 app（含 admin guard）的 TestClient。

    用 create_app 构造真实 app（含 _require_admin_session 守卫 + session 路由），
    这样能测到完整的 cookie 登录 → 守卫校验链路。
    """
    db_path = tmp_path / "test_revocation.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()

    set_engine(
        create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    )
    init_db()

    from bycall.api.app import create_app

    app = create_app()
    return TestClient(app)


def _login(client: TestClient, name: str = "admin", password: str = "admin123") -> None:
    """登录（cookie 自动存在 client 上）。"""
    resp = client.post(
        "/api/login",
        data={"name": name, "password": password},
    )
    assert resp.status_code == 200, resp.text


def _freeze_cookie(client: TestClient) -> str:
    """取出当前 client 的 cookie 值（用于改 DB 后用旧 cookie 重放）。"""
    return client.cookies[SESSION_COOKIE]


def test_admin_disabled_revokes_session(app_client: TestClient):
    """admin 登录拿 cookie → 禁用 admin → 旧 cookie 请求 admin API → 401。"""
    _login(app_client)
    cookie = _freeze_cookie(app_client)

    # 旧 cookie 正常工作
    resp = app_client.get("/api/admin/users")
    assert resp.status_code == 200

    # 禁用 admin（直接改 DB）
    from bycall.db import session_scope
    from bycall.governance.models import User

    with session_scope() as s:
        admin = s.exec(User.select_by_name("admin")).first()
        admin.is_active = False
        s.add(admin)

    # 旧 cookie 立即失效 → 401（用 headers 带旧 cookie，绕过 client 自动管理）
    resp = app_client.get(
        "/api/admin/users", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"}
    )
    assert resp.status_code == 401


def test_admin_password_change_revokes_session(app_client: TestClient):
    """admin 改密 → 旧 cookie 的 password_version 不匹配 → 401。"""
    _login(app_client)
    cookie = _freeze_cookie(app_client)

    # 旧 cookie 正常
    resp = app_client.get("/api/admin/users")
    assert resp.status_code == 200

    # 改密（直接改 DB）
    from bycall.db import session_scope
    from bycall.governance.models import User
    from bycall.governance.security import hash_password

    with session_scope() as s:
        admin = s.exec(User.select_by_name("admin")).first()
        admin.password_hash = hash_password("new-password-456")
        s.add(admin)

    # 旧 cookie 因 password_version 不匹配 → 401
    resp = app_client.get(
        "/api/admin/users", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"}
    )
    assert resp.status_code == 401

    # 新密码登录拿到新 cookie → 正常
    app_client.cookies.clear()
    _login(app_client, password="new-password-456")
    resp = app_client.get("/api/admin/users")
    assert resp.status_code == 200


def test_admin_demoted_revokes_session(app_client: TestClient):
    """admin 降为 member → 旧 cookie 的 role 不再是 admin → 401。"""
    _login(app_client)
    cookie = _freeze_cookie(app_client)

    # 旧 cookie 正常
    resp = app_client.get("/api/admin/users")
    assert resp.status_code == 200

    # 降级（直接改 DB）—— admin role 改成 member
    from bycall.db import session_scope
    from bycall.governance.models import User

    with session_scope() as s:
        admin = s.exec(User.select_by_name("admin")).first()
        admin.role = "member"
        s.add(admin)

    # 旧 cookie：is_admin 仍 True（cookie 缓存），但 DB role=member → 守卫拒绝
    resp = app_client.get(
        "/api/admin/users", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"}
    )
    assert resp.status_code == 401


def test_normal_request_still_works(app_client: TestClient):
    """未篡改、admin 仍有效的 cookie → 200（回归：守卫不误杀正常会话）。"""
    _login(app_client)
    # 多次请求都应正常（守卫每次重查但不影响有效会话）
    for _ in range(3):
        resp = app_client.get("/api/admin/users")
        assert resp.status_code == 200
