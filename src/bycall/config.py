"""分层配置：从 .env / 环境变量加载，所有后端可切换（不硬编码 zvec / openai）。

设计原则：
- 用 pydantic-settings，类型安全 + 自动从环境变量读。
- 嵌套分组（llm / vector_store / postgres / redis），每个子模型有独立 env 前缀，
  因此环境变量是扁平的 ``BYCALL_LLM_*`` / ``BYCALL_VECTOR_STORE_*`` 风格
  （不是 ``BYCALL__LLM__...`` 双下划风格），见 ``.env.example``。
- 敏感字段（api_key）用 SecretStr，避免日志/repr 泄漏。
- 单例 ``get_settings()`` 带 ``lru_cache``，避免重复解析。

新增后端时（如 Qdrant/pgvector）：在 ``VectorStoreConfig.backend`` 的取值里扩展，
并在 ``vector_store/factory.py`` 加一个实现分支即可，业务代码不变。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """模型无关 LLM 配置。M0 支持 openai 兼容 + glm 两个 provider。

    环境变量前缀 ``BYCALL_LLM_``，例：``BYCALL_LLM_PROVIDER`` / ``BYCALL_LLM_API_KEY``。
    """

    model_config = SettingsConfigDict(
        env_prefix="bycall_llm_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    provider: Literal["openai", "glm"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("sk-replace-me")
    model: str = "gpt-4o-mini"
    embed_model: str = "text-embedding-3-small"
    timeout: float = 60.0


class VectorStoreConfig(BaseSettings):
    """向量库配置。backend 预留 pgvector/qdrant 迁移，M0 仅实现 zvec。

    环境变量前缀 ``BYCALL_VECTOR_STORE_``。
    ``dimension`` 必须与 ``LLMConfig.embed_model`` 输出维度对齐
    （如 text-embedding-3-small = 1536）。
    """

    model_config = SettingsConfigDict(
        env_prefix="bycall_vector_store_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    backend: Literal["zvec", "pgvector", "qdrant"] = "zvec"
    data_dir: str = "./data/zvec"
    dimension: int = 1536
    # 只读模式：多进程并发读时打开（zvec 写锁单进程独占，见 PoC step6）。
    read_only: bool = False


class PostgresConfig(BaseSettings):
    """PostgreSQL（状态/记忆/元数据；M0 只起服务）。前缀 ``BYCALL_POSTGRES_``。"""

    model_config = SettingsConfigDict(
        env_prefix="bycall_postgres_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    dsn: str = "postgresql://bycall:bycall@localhost:5432/bycall"
    pool_size: int = 10


class RedisConfig(BaseSettings):
    """Redis（缓存/队列；M0 只起服务）。前缀 ``BYCALL_REDIS_``。"""

    model_config = SettingsConfigDict(
        env_prefix="bycall_redis_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    url: str = "redis://localhost:6379/0"


class IMConfig(BaseSettings):
    """IM 渠道配置。M1.5 接企微智能机器人长连接。前缀 ``BYCALL_IM_``。

    凭证（bot_id/secret）从企业微信后台「智能机器人」获取。无凭证时 enable=False，
    系统不连企微，仅用 Admin 控制台 + /chat 端点测试。
    """

    model_config = SettingsConfigDict(
        env_prefix="bycall_im_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enable: bool = False  # 是否启动 IM 渠道连接
    channel: Literal["wecom", "feishu"] = "wecom"
    # 企微智能机器人凭证（企业微信后台获取）
    wecom_bot_id: str = ""
    wecom_bot_secret: str = ""


class AppConfig(BaseSettings):
    """顶层配置。环境变量前缀 ``BYCALL_``（顶层字段如 ``BYCALL_ENV``）。

    子配置（llm/vector_store/postgres/redis）各自独立读自己的前缀环境变量，
    见各子类 docstring。
    """

    model_config = SettingsConfigDict(
        env_prefix="bycall_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["dev", "prod"] = "dev"
    debug: bool = False
    log_level: str = "INFO"
    # session cookie 签名密钥，生产必改（环境变量 BYCALL_SESSION_SECRET）。
    session_secret: str = "bycall-dev-change-me-in-prod"

    # default_factory 让子模型每次按当前环境变量构造，而非 import 时固化。
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    im: IMConfig = Field(default_factory=IMConfig)

    @model_validator(mode="after")
    def _validate_prod_security(self) -> AppConfig:
        """生产环境 fail-fast：session_secret 必须改为非默认值且 ≥32 字符。

        默认值 ``bycall-dev-change-me-in-prod`` 只能用于 dev/test。生产启动时若仍是
        默认值或太短，cookie 签名可被伪造——直接拒绝启动（比"启动了但不安全"好）。
        """
        if self.env != "prod":
            return self
        if self.session_secret == "bycall-dev-change-me-in-prod":
            raise ValueError(
                "生产环境（env=prod）必须设置 BYCALL_SESSION_SECRET（不能是默认值）。"
                "请生成随机密钥：python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if len(self.session_secret) < 32:
            raise ValueError(
                f"生产环境 session_secret 长度不足（当前 {len(self.session_secret)}，"
                "至少 32 字符）。请用 secrets.token_urlsafe(48) 生成。"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppConfig:
    """返回单例配置。测试里调 ``get_settings.cache_clear()`` 重置。"""
    return AppConfig()
