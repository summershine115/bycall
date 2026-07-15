"""数据库敏感字段必须以密文保存，同时业务读取保持明文语义。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.governance.models import Model, SystemSetting
from bycall.governance.secrets import decrypt_secret, encrypt_secret, is_encrypted_secret
from bycall.governance.settings_store import get_setting_value, set_setting_value


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "secrets.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    return db_path


@pytest.fixture
def client(db_env):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_secret_roundtrip_and_ciphertext_differs():
    encrypted = encrypt_secret("bot-secret-value")
    assert is_encrypted_secret(encrypted)
    assert "bot-secret-value" not in encrypted
    assert decrypt_secret(encrypted) == "bot-secret-value"


def test_legacy_plaintext_is_readable():
    assert decrypt_secret("legacy-plain-value") == "legacy-plain-value"


def test_setting_secret_is_encrypted_at_rest(db_env):
    from bycall.db import get_engine

    with Session(get_engine()) as session:
        set_setting_value(session, "im.wecom_bot_secret", "real-secret", is_secret=True)
        session.commit()

        row = session.exec(
            select(SystemSetting).where(SystemSetting.key == "im.wecom_bot_secret")
        ).first()
        assert row is not None
        assert is_encrypted_secret(row.value)
        assert "real-secret" not in row.value
        assert get_setting_value("im.wecom_bot_secret", session=session) == "real-secret"


def test_model_api_key_is_encrypted_at_rest(client):
    response = client.post(
        "/api/admin/models",
        json={
            "name": "encrypted-model",
            "model_type": "chat",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-real-secret-value",
        },
    )
    assert response.status_code == 201

    from bycall.db import get_engine

    with Session(get_engine()) as session:
        model = session.exec(select(Model).where(Model.name == "encrypted-model")).first()
        assert model is not None
        assert is_encrypted_secret(model.api_key)
        assert "sk-real-secret-value" not in model.api_key
        assert decrypt_secret(model.api_key) == "sk-real-secret-value"
