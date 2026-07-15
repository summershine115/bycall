"""配置加载测试：环境变量覆盖、嵌套字段、SecretStr 保护。"""

from __future__ import annotations

import pytest

from bycall.config import AppConfig, get_settings


def test_default_config_loads():
    """无环境变量时，所有字段有合理默认值（可空启动）。"""
    config = get_settings()
    assert isinstance(config, AppConfig)
    assert config.env in ("dev", "prod")
    assert config.vector_store.backend == "zvec"
    assert config.vector_store.dimension > 0


def test_env_override_top_level(monkeypatch):
    """顶层字段 ``BYCALL_ENV`` / ``BYCALL_DEBUG`` 可被环境变量覆盖。"""
    monkeypatch.setenv("BYCALL_ENV", "prod")
    monkeypatch.setenv("BYCALL_DEBUG", "false")
    monkeypatch.setenv("BYCALL_SECRET_ENCRYPTION_KEY", "e" * 48)
    get_settings.cache_clear()
    config = get_settings()
    assert config.env == "prod"
    assert config.debug is False


def test_env_override_nested(monkeypatch):
    """嵌套字段通过双下划分隔读取（``BYCALL_LLM__MODEL`` → llm.model）。

    注：pydantic-settings 嵌套模型默认读 ``BYCALL_LLM_MODEL``（无分隔符），
    env_nested_delimiter 只影响显式嵌套。这里验证扁平别名也能命中 llm 子模型。
    """
    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "glm")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_BACKEND", "zvec")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "768")
    get_settings.cache_clear()
    config = get_settings()
    assert config.llm.provider == "glm"
    assert config.vector_store.dimension == 768


def test_api_key_is_secretstr(monkeypatch):
    """api_key 用 SecretStr 包裹，str(config) 不应泄漏明文。"""
    monkeypatch.setenv("BYCALL_LLM_API_KEY", "sk-super-secret-123")
    get_settings.cache_clear()
    config = get_settings()
    # SecretStr 的 repr 是 SecretStr('**********')
    assert "sk-super-secret-123" not in repr(config)
    assert config.llm.api_key.get_secret_value() == "sk-super-secret-123"


def test_invalid_provider_rejected(monkeypatch):
    """provider 取值约束：只允许 openai / glm。"""
    from pydantic import ValidationError

    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "claude")
    get_settings.cache_clear()
    # pydantic 对 Literal 校验失败会抛 ValidationError。
    with pytest.raises(ValidationError):
        get_settings()
