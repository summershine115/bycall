"""认证与会话单元测试：cookie 签名往返、登录、dev act-as-user。

覆盖：
- cookie 签名/验签往返、篡改、过期
- login 正确/错误密码/禁用用户
- set_act_as 在 dev/prod、非 admin、目标 admin 下的行为
"""

from __future__ import annotations

import time

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.governance.auth import (
    SessionData,
    load_session_cookie,
    login,
    make_session_cookie,
    password_version,
    set_act_as,
)
from bycall.governance.security import hash_password


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """临时 SQLite + init_db，dev 环境。

    用 set_engine 注入独立引擎，避免与其他测试共享全局引擎。
    """
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()
    return tmp_path


def _make_plain_user(session, name: str = "alice", role: str = "member", is_active: bool = True):
    """创建一个普通用户（非 admin），返回 User 实例。"""
    from bycall.governance.models import User

    user = User(
        name=name,
        display_name=name.title(),
        role=role,
        password_hash=hash_password("pw-" + name),
        is_active=is_active,
    )
    session.add(user)
    session.flush()  # 拿到 id
    return user


# ============ cookie 签名往返 ============


def test_session_cookie_roundtrip():
    """make_session_cookie → load_session_cookie 能还原 SessionData。"""
    data = SessionData(user_id=1, user_name="admin", role="admin", is_admin=True)
    token = make_session_cookie(data)
    restored = load_session_cookie(token)
    assert restored == data


def test_session_cookie_roundtrip_with_act_as():
    """带 act_as_user_id 的往返也能还原（含 None 分支）。"""
    data = SessionData(user_id=1, user_name="admin", role="admin", is_admin=True, act_as_user_id=42)
    token = make_session_cookie(data)
    restored = load_session_cookie(token)
    assert restored == data
    assert restored.act_as_user_id == 42


def test_tampered_token_returns_none():
    """篡改的 token → None。"""
    token = make_session_cookie(
        SessionData(user_id=1, user_name="admin", role="admin", is_admin=True)
    )
    # 破坏签名尾部（base64 字符），验签必失败。
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    assert load_session_cookie(tampered) is None


def test_expired_token_returns_none(monkeypatch):
    """过期 token → None。

    itsdangerous 签名时间戳取自 ``time.time()``：签名时把时钟拨回 60s，
    即可确定性地构造已过期 token（避免真睡 2.1s 等整秒粒度时间戳翻页）。
    """
    real_now = time.time()
    monkeypatch.setattr(time, "time", lambda: real_now - 60)
    token = make_session_cookie(
        SessionData(user_id=1, user_name="admin", role="admin", is_admin=True)
    )
    monkeypatch.undo()
    assert load_session_cookie(token, max_age=1) is None


# ============ login ============


def test_login_correct_password(db_env):
    """admin 账号 + 正确密码 → SessionData，is_admin=True。"""
    from bycall.db import session_scope

    with session_scope() as s:
        result = login(s, "admin", "admin123")
    assert result is not None
    assert result.is_admin is True
    assert result.user_name == "admin"
    assert result.role == "admin"
    assert result.act_as_user_id is None


def test_login_wrong_password(db_env):
    """错误密码 → None。"""
    from bycall.db import session_scope

    with session_scope() as s:
        result = login(s, "admin", "wrong-password")
    assert result is None


def test_login_disabled_user(db_env):
    """禁用用户（is_active=False）→ None，即便密码正确。"""
    from bycall.db import session_scope
    from bycall.governance.models import User

    with session_scope() as s:
        # 把 admin 禁用
        admin = s.exec(User.select_by_name("admin")).first()
        admin.is_active = False
        s.add(admin)
        result = login(s, "admin", "admin123")
    assert result is None


def test_login_nonexistent_user(db_env):
    """不存在的用户 → None。"""
    from bycall.db import session_scope

    with session_scope() as s:
        result = login(s, "nobody", "whatever")
    assert result is None


# ============ set_act_as ============


def _get_admin(session):
    from bycall.governance.models import User

    return session.exec(User.select_by_name("admin")).first()


def test_set_act_as_dev(db_env):
    """dev 环境：admin 扮演普通用户 → act_as_user_id 正确。"""
    from bycall.db import session_scope

    with session_scope() as s:
        admin = _get_admin(s)
        target = _make_plain_user(s, name="alice")
        admin_id, target_id = admin.id, target.id
        result = set_act_as(s, admin, target.id)
    assert result.act_as_user_id == target_id
    assert result.user_id == admin_id
    assert result.is_admin is True


def test_set_act_as_prod_raises(db_env, monkeypatch):
    """prod 环境 → PermissionError（monkeypatch config.env='prod'，先 cache_clear）。"""
    from bycall.db import session_scope

    monkeypatch.setenv("BYCALL_ENV", "prod")
    monkeypatch.setenv("BYCALL_SECRET_ENCRYPTION_KEY", "e" * 48)
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "test-session-secret-" + "s" * 32)
    get_settings.cache_clear()
    assert get_settings().env == "prod"

    with session_scope() as s:
        admin = _get_admin(s)
        target = _make_plain_user(s, name="alice")
        with pytest.raises(PermissionError):
            set_act_as(s, admin, target.id)


def test_set_act_as_non_admin_raises(db_env):
    """非 admin 用户调用 → PermissionError。"""
    from bycall.db import session_scope

    with session_scope() as s:
        alice = _make_plain_user(s, name="alice")
        bob = _make_plain_user(s, name="bob")
        with pytest.raises(PermissionError):
            set_act_as(s, alice, bob.id)


def test_set_act_as_target_is_admin_raises(db_env):
    """目标是 admin → PermissionError（不能扮演另一个 admin）。"""
    from bycall.db import session_scope

    with session_scope() as s:
        admin = _get_admin(s)
        # 扮演自己（admin）应被拒
        with pytest.raises(PermissionError):
            set_act_as(s, admin, admin.id)


def test_set_act_as_target_inactive_raises(db_env):
    """目标 is_active=False → PermissionError。"""
    from bycall.db import session_scope

    with session_scope() as s:
        admin = _get_admin(s)
        inactive = _make_plain_user(s, name="ghost", is_active=False)
        with pytest.raises(PermissionError):
            set_act_as(s, admin, inactive.id)


# ============ clear_act_as ============


def test_clear_act_as_returns_admin_session(db_env):
    """clear_act_as 回到 admin 身份（act_as_user_id=None）。"""
    from bycall.db import session_scope
    from bycall.governance.auth import clear_act_as

    with session_scope() as s:
        admin = _get_admin(s)
        admin_id = admin.id
        result = clear_act_as(s, admin)
    assert result.act_as_user_id is None
    assert result.user_id == admin_id
    assert result.is_admin is True


def test_password_version_uses_full_hash_not_bcrypt_prefix():
    """bcrypt 固定前缀相同也必须得到不同会话版本。"""
    first = "$2b$12$a" + "x" * 52
    second = "$2b$12$a" + "y" * 52
    assert first[:8] == second[:8]
    assert password_version(first) != password_version(second)
