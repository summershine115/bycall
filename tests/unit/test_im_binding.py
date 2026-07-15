"""IM 身份绑定闭环测试（P1-7：绑定无唯一约束，重复绑定可能映射错误用户）。

验证 UserCreate/UserUpdate 支持 im_bindings 字段，且 (channel, im_user_id) 全局唯一。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """完整 app TestClient。"""
    db_path = tmp_path / "test_binding.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()

    set_engine(
        create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    )
    init_db()

    from bycall.api.app import create_app

    app = create_app()
    c = TestClient(app)
    c.post("/api/login", data={"name": "admin", "password": "admin123"})
    return c


def test_create_user_with_im_binding(client: TestClient):
    """创建用户带 im_bindings → 201 + 绑定写入。"""
    resp = client.post(
        "/api/admin/users",
        json={
            "name": "alice",
            "role": "member",
            "im_bindings": {"wecom": "wecom_alice_id"},
        },
    )
    assert resp.status_code == 201
    uid = resp.json()["id"]
    detail = client.get(f"/api/admin/users/{uid}").json()
    assert detail["im_bindings"] == {"wecom": "wecom_alice_id"}


def test_duplicate_im_binding_rejected(client: TestClient):
    """同一 (wecom, userA) 绑到第二个用户 → 409。"""
    # 第一个用户绑定 wecom:wecom_alice_id
    resp1 = client.post(
        "/api/admin/users",
        json={
            "name": "alice",
            "role": "member",
            "im_bindings": {"wecom": "wecom_alice_id"},
        },
    )
    assert resp1.status_code == 201

    # 第二个用户尝试绑同一个 IM 账号 → 409
    resp2 = client.post(
        "/api/admin/users",
        json={
            "name": "bob",
            "role": "member",
            "im_bindings": {"wecom": "wecom_alice_id"},  # 冲突
        },
    )
    assert resp2.status_code == 409
    assert "已绑定" in resp2.json()["detail"]


def test_update_user_im_binding(client: TestClient):
    """更新用户 im_bindings → 200 + 绑定更新。"""
    uid = client.post(
        "/api/admin/users",
        json={"name": "carol", "role": "member"},
    ).json()["id"]

    resp = client.patch(
        f"/api/admin/users/{uid}",
        json={"im_bindings": {"feishu": "feishu_carol"}},
    )
    assert resp.status_code == 200
    assert resp.json()["im_bindings"] == {"feishu": "feishu_carol"}


def test_update_duplicate_binding_rejected(client: TestClient):
    """更新时绑定到已被占用的 IM 账号 → 409。"""
    # alice 绑 wecom:shared_id
    client.post(
        "/api/admin/users",
        json={"name": "alice", "role": "member", "im_bindings": {"wecom": "shared_id"}},
    )
    # bob 不带绑定
    bob_id = client.post(
        "/api/admin/users", json={"name": "bob", "role": "member"}
    ).json()["id"]

    # bob 尝试抢 alice 的绑定 → 409
    resp = client.patch(
        f"/api/admin/users/{bob_id}",
        json={"im_bindings": {"wecom": "shared_id"}},
    )
    assert resp.status_code == 409


def test_same_user_rebind_same_channel_allowed(client: TestClient):
    """用户重新绑定自己已有的 channel（改值）→ 允许（不与自己冲突）。"""
    uid = client.post(
        "/api/admin/users",
        json={"name": "dave", "role": "member", "im_bindings": {"wecom": "old_id"}},
    ).json()["id"]

    resp = client.patch(
        f"/api/admin/users/{uid}",
        json={"im_bindings": {"wecom": "new_id"}},  # 改自己的绑定值
    )
    assert resp.status_code == 200
    assert resp.json()["im_bindings"] == {"wecom": "new_id"}


def test_different_channels_allowed(client: TestClient):
    """不同 channel 的相同 im_user_id 允许共存（企微和飞书各一个）。"""
    client.post(
        "/api/admin/users",
        json={"name": "eve", "role": "member", "im_bindings": {"wecom": "same_id"}},
    )
    resp = client.post(
        "/api/admin/users",
        json={"name": "frank", "role": "member", "im_bindings": {"feishu": "same_id"}},
    )
    assert resp.status_code == 201  # 不同 channel，不冲突
