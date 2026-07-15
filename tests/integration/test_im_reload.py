"""IM 渠道热重载 API 测试。

验证：reload 不重启服务、status 查询、无凭证时停止渠道。
（不测真实企微连接——凭证用假的，验证的是热重载的生命周期逻辑。）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.gateway.manager import reset_channel_manager


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_IM_ENABLE", "false")  # 初始不启用
    get_settings.cache_clear()
    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()
    reset_channel_manager()
    from bycall.api.app import create_app

    app = create_app()
    c = TestClient(app)
    c.post("/api/login", data={"name": "admin", "password": "admin123"})
    yield c
    reset_channel_manager()


def test_im_status_disabled(client):
    """IM 未启用 → status running=false。"""
    resp = client.get("/api/admin/im/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False


def test_reload_without_credentials_stops(client):
    """无凭证 reload → 成功停止（不报错），返回未启动消息。"""
    resp = client.post("/api/admin/im/reload")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert "未启用" in resp.json()["message"] or "停止" in resp.json()["message"]


def test_reload_with_fake_credentials(client):
    """填假凭证 reload → 尝试建连（会失败但不 crash），ok=True。

    这里验证的是生命周期逻辑（stop+start 被调用），不是真实连接成功。
    """
    client.put(
        "/api/admin/settings",
        json={
            "settings": {
                "im.enable": "true",
                "im.wecom_bot_id": "fake_id",
                "im.wecom_bot_secret": "fake_secret",
            }
        },
    )
    resp = client.post("/api/admin/im/reload")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # 有凭证 → 尝试启动（消息含"已启动"或"已重连"）
    assert "已" in resp.json()["message"]


def test_reload_requires_admin(client):
    """未登录 → 401。"""
    bare = TestClient(client.app)
    resp = bare.post("/api/admin/im/reload")
    assert resp.status_code == 401
