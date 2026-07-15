"""生产环境 fail-fast 配置校验测试（P1-2：默认凭证 + 固定 session_secret）。

验证 env=prod 时 get_settings() 在以下情况直接抛 ValidationError：
- session_secret 仍是默认值
- session_secret 太短（< 32）
dev 环境不受约束。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bycall.config import get_settings


def test_prod_default_secret_rejected(monkeypatch):
    """prod + 默认 session_secret → ValidationError。"""
    monkeypatch.setenv("BYCALL_ENV", "prod")
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "bycall-dev-change-me-in-prod")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="不能是默认值"):
        get_settings()


def test_prod_short_secret_rejected(monkeypatch):
    """prod + 短 session_secret（<32）→ ValidationError。"""
    monkeypatch.setenv("BYCALL_ENV", "prod")
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "short-but-not-default-key!")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="长度不足"):
        get_settings()


def test_prod_valid_secret_accepted(monkeypatch):
    """prod + 合法 session_secret（≥32 且非默认）→ 正常构造。"""
    monkeypatch.setenv("BYCALL_ENV", "prod")
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "a" * 48)  # 48 字符随机级密钥（测试用重复字符）
    monkeypatch.setenv("BYCALL_SECRET_ENCRYPTION_KEY", "b" * 48)
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.env == "prod"
    assert settings.session_secret == "a" * 48


def test_prod_missing_encryption_key_rejected(monkeypatch):
    monkeypatch.setenv("BYCALL_ENV", "prod")
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "a" * 48)
    monkeypatch.delenv("BYCALL_SECRET_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="SECRET_ENCRYPTION_KEY"):
        get_settings()


def test_dev_default_secret_allowed(monkeypatch):
    """dev + 默认 session_secret → 不报错（开发便利）。"""
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "bycall-dev-change-me-in-prod")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.env == "dev"
    assert settings.session_secret == "bycall-dev-change-me-in-prod"


def test_dev_short_secret_allowed(monkeypatch):
    """dev + 短 session_secret → 不报错。"""
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_SESSION_SECRET", "short")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.session_secret == "short"
