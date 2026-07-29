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
    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()
    from bycall.api.app import create_app

    app = create_app()
    c = TestClient(app)
    c.post("/api/login", data={"name": "admin", "password": "admin123"})
    return c


def test_list_settings_masks_im_secrets(client):
    """/settings 返回的 IM secret 字段脱敏（绝不回显明文）。"""
    client.put(
        "/api/admin/settings", json={"settings": {"im.wecom_bot_secret": "bot-secret-12345678"}}
    )
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


def test_empty_non_secret_value_clears_setting(client):
    """非秘密字段（bot_id）传空串 → 清除该设置（回退 .env/默认）。"""
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_id": "bot-x"}})
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_id": ""}})
    resp = client.get("/api/admin/settings")
    bot_id = next(s for s in resp.json() if s["key"] == "im.wecom_bot_id")
    assert bot_id["value"] == ""


def test_empty_secret_value_keeps_unchanged(client):
    """secret 传空串 → 视为未改（不清除）。

    前端对「未改」的 secret 回传空串（Settings.vue「不改则留空保存」），后端必须
    跳过而非清除，否则每次保存其它设置都会把已配置 secret 冲掉（曾导致企微 853000）。
    """
    client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_secret": "real-bot-secret"}})
    resp = client.put("/api/admin/settings", json={"settings": {"im.wecom_bot_secret": ""}})
    assert resp.status_code == 200
    assert any("im.wecom_bot_secret" in s for s in resp.json()["skipped"])
    from bycall.db import session_scope
    from bycall.governance.settings_store import get_setting_value

    with session_scope() as s:
        assert get_setting_value("im.wecom_bot_secret", session=s) == "real-bot-secret"


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


# ============ 解析引擎配置（与 IM 同机制：控制台可改、即时生效）============


def test_parse_settings_seeded_and_listed(client):
    """init_db 灌入 parse.* 默认 key；mineru token 按 secret 脱敏回显。"""
    resp = client.get("/api/admin/settings")
    settings = {s["key"]: s for s in resp.json()}
    assert settings["parse.engine"]["value"] == "builtin"
    assert settings["parse.mineru_api_token"]["is_secret"] is True


def test_update_parse_engine_takes_effect(client):
    """控制台改 parse.engine → 引擎目录默认标记即时切换（DB 优先，无需重启）。"""
    resp = client.put("/api/admin/settings", json={"settings": {"parse.engine": "mineru"}})
    assert resp.status_code == 200
    assert "parse.engine" in resp.json()["updated"]
    engines = {e["name"]: e for e in client.get("/api/admin/parse-engines").json()}
    assert engines["mineru"]["is_default"] is True
    assert engines["builtin"]["is_default"] is False


def test_update_mineru_base_url_enables_engine(client):
    """配 mineru_base_url → mineru 引擎变可用（可用性实时反映配置）。"""
    client.put(
        "/api/admin/settings",
        json={"settings": {"parse.mineru_base_url": "http://mineru.local:8000"}},
    )
    engines = {e["name"]: e for e in client.get("/api/admin/parse-engines").json()}
    assert engines["mineru"]["is_available"] is True


def test_invalid_parse_engine_rejected(client):
    """非法引擎名 → 4xx 拒绝（不入库，保持原值 builtin）。"""
    resp = client.put("/api/admin/settings", json={"settings": {"parse.engine": "turbo"}})
    assert resp.status_code == 400
    engine = next(s for s in client.get("/api/admin/settings").json() if s["key"] == "parse.engine")
    assert engine["value"] == "builtin"


def test_invalid_parse_number_rejected(client):
    """timeout 非数字 / 负数 → 4xx 拒绝。"""
    for bad in ("abc", "-5", "0"):
        resp = client.put("/api/admin/settings", json={"settings": {"parse.mineru_timeout": bad}})
        assert resp.status_code == 400


def test_invalid_parse_method_rejected(client):
    """非法 parse_method → 4xx 拒绝。"""
    resp = client.put(
        "/api/admin/settings", json={"settings": {"parse.mineru_parse_method": "fancy"}}
    )
    assert resp.status_code == 400


def test_mineru_token_secret_masked(client):
    """mineru token 按 secret 处理：回显脱敏、空串提交不清除。"""
    client.put(
        "/api/admin/settings",
        json={"settings": {"parse.mineru_api_token": "tok-1234567890"}},
    )
    resp = client.get("/api/admin/settings")
    token = next(s for s in resp.json() if s["key"] == "parse.mineru_api_token")
    assert "tok-1234567890" not in token["value"]
    assert token["is_set"] is True
    # 空串 → 跳过（不清除已配 token），与 IM secret 一致
    resp = client.put("/api/admin/settings", json={"settings": {"parse.mineru_api_token": ""}})
    assert any("parse.mineru_api_token" in s for s in resp.json()["skipped"])


def test_mineru_cloud_settings_seeded(client):
    """新 key（mode / model_version / is_ocr）首次启动即 seed，默认值正确。"""
    settings = {s["key"]: s for s in client.get("/api/admin/settings").json()}
    assert settings["parse.mineru_mode"]["value"] == "selfhost"
    assert settings["parse.mineru_model_version"]["value"] == "vlm"
    assert settings["parse.mineru_is_ocr"]["value"] == "false"
    assert settings["parse.mineru_is_ocr"]["is_secret"] is False


def test_cloud_mode_available_with_token(client):
    """切 cloud + 配 token → mineru 引擎可用（可用性只看 token，不看 base_url）。"""
    client.put(
        "/api/admin/settings",
        json={"settings": {"parse.mineru_mode": "cloud", "parse.mineru_api_token": "tok-abcdefgh"}},
    )
    engines = {e["name"]: e for e in client.get("/api/admin/parse-engines").json()}
    assert engines["mineru"]["is_available"] is True
    assert "token" in engines["mineru"]["description"]


def test_selfhost_mode_unavailable_without_base_url(client):
    """selfhost + 无 base_url → 不可用，原因指向 base_url（与 cloud 的 token 判据区分）。"""
    engines = {e["name"]: e for e in client.get("/api/admin/parse-engines").json()}
    assert engines["mineru"]["is_available"] is False
    assert "BASE_URL" in engines["mineru"]["reason_unavailable"]


def test_invalid_mineru_mode_and_model_rejected(client):
    """非法 mode / model_version / is_ocr → 4xx 拒绝（保持原值）。"""
    for key, bad in (
        ("parse.mineru_mode", "onprem"),
        ("parse.mineru_model_version", "gpt-4"),
        ("parse.mineru_is_ocr", "maybe"),
    ):
        resp = client.put("/api/admin/settings", json={"settings": {key: bad}})
        assert resp.status_code == 400, f"{key}={bad} 应被拒绝"
