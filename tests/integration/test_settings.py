"""运行时设置 API 集成测试 —— LLM/IM 凭证控制台管理。

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


def test_list_settings_masks_secrets(client):
    """/settings 返回的 secret 字段脱敏（绝不回显明文）。"""
    # 先设一个 api_key
    client.put("/api/admin/settings", json={"settings": {"llm.api_key": "sk-secret-12345678"}})
    resp = client.get("/api/admin/settings")
    assert resp.status_code == 200
    settings = {s["key"]: s for s in resp.json()}
    api_key = settings["llm.api_key"]
    assert api_key["is_secret"] is True
    assert "sk-secret-12345678" not in api_key["value"]  # 明文绝不出现
    assert "*" in api_key["value"]  # 脱敏了
    assert api_key["is_set"] is True
    # 非 secret 不脱敏
    assert settings["llm.provider"]["is_secret"] is False


def test_update_llm_provider_takes_effect(client):
    """改 llm.provider 后，factory 读到新值（即时生效验证）。"""
    client.put("/api/admin/settings", json={"settings": {"llm.provider": "glm"}})
    from bycall.db import session_scope
    from bycall.governance.settings_store import get_setting_value

    with session_scope() as s:
        assert get_setting_value("llm.provider", session=s) == "glm"


def test_secret_placeholder_skipped(client):
    """secret 字段提交含 *** 的脱敏占位符 → 视为未改，跳过。"""
    # 先设真值
    client.put("/api/admin/settings", json={"settings": {"llm.api_key": "sk-real-value"}})
    # 再提交脱敏占位符（列表读回的脱敏值）
    resp = client.get("/api/admin/settings")
    masked = next(s["value"] for s in resp.json() if s["key"] == "llm.api_key")
    # 提交这个脱敏值 → 应被跳过，原值不变
    resp = client.put(
        "/api/admin/settings",
        json={"settings": {"llm.api_key": masked}},
    )
    assert resp.status_code == 200
    assert "llm.api_key" in resp.json()["skipped"] or "llm.api_key" not in resp.json()["updated"]
    # 原值还在
    from bycall.db import session_scope
    from bycall.governance.settings_store import get_setting_value

    with session_scope() as s:
        assert get_setting_value("llm.api_key", session=s) == "sk-real-value"


def test_empty_value_clears_setting(client):
    """secret 传空串 → 清除该设置（回退到 .env/默认）。"""
    client.put("/api/admin/settings", json={"settings": {"llm.api_key": "sk-x"}})
    client.put("/api/admin/settings", json={"settings": {"llm.api_key": ""}})
    resp = client.get("/api/admin/settings")
    api_key = next(s for s in resp.json() if s["key"] == "llm.api_key")
    assert api_key["is_set"] is False


def test_unknown_key_rejected(client):
    """未知 key → skipped（不创建新 key）。"""
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
