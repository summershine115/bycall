"""运行时设置 API 集成测试 —— IM 凭证控制台管理。

LLM 配置已迁移到 Model 实体（test_models_api.py 测），SystemSetting 现在只管 IM。
验证：列表脱敏、更新即时生效、secret 占位符保护、清除设置。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}))
    init_db()
    from bycall.api.app import create_app

    app = create_app()
    c = TestClient(app)
    c.post("/api/login", data={"name": "admin", "password": "admin123"})
    return c


def test_list_settings_masks_im_secrets(client):
    """/settings 返回的 IM secret 字段脱敏（绝不回显明文）。"""
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_secret": "bot-secret-12345678"}})
    resp = client.get("/api/admin/settings")
    assert resp.status_code == 200
    settings = {s["key"]: s for s in resp.json()}
    secret = settings["im.wecom_bot_secret"]
    assert secret["is_secret"] is True
    assert "bot-secret-12345678" not in secret["value"]
    assert "*" in secret["value"]
    assert secret["is_set"] is True
    # 非 secret（bot_id）不脱敏
    assert settings["im.wecom_bot_id"]["is_secret"] is False


def test_update_im_setting_takes_effect(client):
    """改 im.wecom_bot_id 后即时生效（DB 值更新）。"""
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_id": "new_bot_001"}})
    from bycall.db import session_scope
    from bycall.governance.settings_store import get_setting_value

    with session_scope() as s:
        assert get_setting_value("im.wecom_bot_id", session=s) == "new_bot_001"


def test_secret_placeholder_skipped(client):
    """secret 字段提交含 *** 的脱敏占位符 → 视为未改，跳过。"""
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_secret": "real-bot-secret"}})
    resp = client.get("/api/admin/settings")
    masked = next(s["value"] for s in resp.json() if s["key"] == "im.wecom_bot_secret")
    resp = client.put(
        "/api/admin/settings",
        json={"settings": {"im.wecom_bot_secret": masked}},
    )
    assert resp.status_code == 200
    skipped = resp.json()["skipped"]
    assert any("im.wecom_bot_secret" in s for s in skipped)
    from bycall.db import session_scope
    from bycall.governance.settings_store import get_setting_value

    with session_scope() as s:
        assert get_setting_value("im.wecom_bot_secret", session=s) == "real-bot-secret"


def test_empty_value_clears_setting(client):
    """secret 传空串 → 清除该设置。"""
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_secret": "bot-x"}})
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_secret": ""}})
    resp = client.get("/api/admin/settings")
    secret = next(s for s in resp.json() if s["key"] == "im.wecom_bot_secret")
    assert secret["is_set"] is False


def test_unknown_key_rejected(client):
    """未知 key → skipped。"""
    resp = client.put(
        "/api/admin/settings",
        json={"settings": {"nonexistent.key": "value"}},
    )
    skipped = resp.json()["skipped"]
    assert any("nonexistent.key" in s for s in skipped)


def test_settings_require_admin(client):
    """未登录访问 /settings → 401。"""
    bare = TestClient(client.app)
    resp = bare.get("/api/admin/settings")
    assert resp.status_code == 401
