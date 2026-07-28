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
        env_prefix="bycall_vector_store_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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


class ObservabilityConfig(BaseSettings):
    """可观测性配置（M5）。前缀 ``BYCALL_OTEL_``。

    OpenTelemetry 全链路 trace + 结构化日志（correlation_id 贯穿）。默认关闭：
    ``enabled=False`` 时 ``setup_observability()`` 完全 no-op，不影响现有行为与测试。
    生产经环境变量启用，OTLP endpoint 指向自建 collector（内网无公网）。
    """

    model_config = SettingsConfigDict(
        env_prefix="bycall_otel_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = False
    # OTLP endpoint（gRPC 或 HTTP，如 http://otel-collector:4317）。空 → 仅 console exporter（开发用）。
    otlp_endpoint: str = ""
    service_name: str = "bycall"
    # head-based 采样率，1.0 = 全采样。
    sampler_ratio: float = 1.0
    # 结构化日志：True→JSON（生产），False→人类可读带 correlation_id（开发）。
    structured_logs: bool = False


class ParseConfig(BaseSettings):
    """文档解析引擎配置。前缀 ``BYCALL_PARSE_``。

    解析引擎可切换（参考 ``VectorStoreConfig.backend`` 的可切换设计）：
    - ``builtin``：按文件扩展名路由的本地解析器（MuPDF/python-docx/bs4/...，零外部依赖）。
    - ``markitdown``：Microsoft MarkItDown 通用解析器（需 ``pip install markitdown``）。
    - ``mineru``：MinerU 远程 API（布局识别 + OCR，需自建或接入 MinerU 服务）。

    非默认引擎**不可用**（依赖未装 / 远程未配置）时，``parse_document`` 对**其不支持的类型**
    自动退回 ``builtin``——保证不回归。仅当引擎声明支持该类型却解析失败时才报错。
    """

    model_config = SettingsConfigDict(
        env_prefix="bycall_parse_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    engine: Literal["builtin", "markitdown", "mineru"] = "builtin"
    # ---- MarkItDown（本地库）----
    # 启用第三方插件（pip install 的 markitdown 插件）；默认关闭，只用内置 converter。
    markitdown_enable_plugins: bool = False
    # ---- MinerU 远程 API（自建 mineru-api 或 mineru.net 云端）----
    # 服务基地址，如 ``https://mineru.net`` 或自建 ``http://127.0.0.1:8000``。空 → 引擎不可用。
    mineru_base_url: str = ""
    # 鉴权 token（Bearer）。mineru.net 云端必填；多数自建部署可留空。
    mineru_api_token: SecretStr = SecretStr("")
    # 解析后端（MinerU --backend）：pipeline / vlm-auto-engine / vlm-http-client /
    # hybrid-auto-engine / hybrid-http-client。自建服务按其算力选；云端用 hybrid-*。
    mineru_backend: str = "hybrid-auto-engine"
    # 解析方法（MinerU --parse_method）：auto / txt / ocr。仅 pipeline/hybrid 生效。
    mineru_parse_method: Literal["auto", "txt", "ocr"] = "auto"
    # OCR 语言组（仅 pipeline/hybrid 生效），如 ch / en / korean / japan。
    mineru_lang: str = "ch"
    # 单任务最长等待秒（提交→完成）。远程解析较慢，默认 10 分钟。
    mineru_timeout: float = 600.0
    # 轮询任务状态间隔秒。
    mineru_poll_interval: float = 2.0


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
    # DB 敏感字段加密根密钥。dev 为空时回退 session_secret；prod 必须独立配置。
    secret_encryption_key: SecretStr = SecretStr("")
    # 仅首次生产启动且 DB 尚无 admin 时使用；创建成功后可从环境移除。
    bootstrap_admin_password: SecretStr = SecretStr("")

    # default_factory 让子模型每次按当前环境变量构造，而非 import 时固化。
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    parse: ParseConfig = Field(default_factory=ParseConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    im: IMConfig = Field(default_factory=IMConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

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
                '请生成随机密钥：python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if len(self.session_secret) < 32:
            raise ValueError(
                f"生产环境 session_secret 长度不足（当前 {len(self.session_secret)}，"
                "至少 32 字符）。请用 secrets.token_urlsafe(48) 生成。"
            )
        encryption_key = self.secret_encryption_key.get_secret_value()
        if len(encryption_key) < 32:
            raise ValueError(
                "生产环境必须设置至少 32 字符的 BYCALL_SECRET_ENCRYPTION_KEY，"
                "且应与 BYCALL_SESSION_SECRET 不同。"
            )
        if encryption_key == self.session_secret:
            raise ValueError("生产环境的密钥加密根密钥不能与 session_secret 相同。")
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppConfig:
    """返回单例配置。测试里调 ``get_settings.cache_clear()`` 重置。"""
    return AppConfig()
