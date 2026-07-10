"""LLM 客户端工厂：按设置（DB 优先 > .env > 默认）实例化 provider。

Bycall 的配置真值在 DB（admin 在控制面改），所以这里每次都读当前 DB 值——
admin 改 key 后下一条消息即时生效，无需重启。
（OpenClaw 热重载的体验，但实现更简单：DB 即真值。）
"""

from __future__ import annotations

from sqlmodel import Session

from bycall.llm.base import LLMClient


def create_llm_client(config=None, *, session: Session | None = None) -> LLMClient:
    """实例化一个 LLMClient。

    优先级：DB 设置（admin 控制）> 传入 config（.env/pydantic）> 代码默认。
    admin 改 DB 后下一条消息即时生效。
    """
    from bycall.governance.settings_store import get_setting_value

    # config 兜底值（来自 .env / 测试用 LLMConfig）。用 getattr 容错（部分测试传 SimpleNamespace）。
    cfg_provider = getattr(config, "provider", "openai")
    cfg_base_url = getattr(config, "base_url", "https://api.openai.com/v1")
    _key = getattr(config, "api_key", None)
    cfg_api_key = _key.get_secret_value() if hasattr(_key, "get_secret_value") else str(_key or "")
    cfg_model = getattr(config, "model", "gpt-4o-mini")
    cfg_embed_model = getattr(config, "embed_model", "text-embedding-3-small")
    cfg_timeout = getattr(config, "timeout", 60.0)

    provider = get_setting_value(
        "llm.provider", default=cfg_provider, env_var="BYCALL_LLM_PROVIDER", session=session
    )
    base_url = get_setting_value(
        "llm.base_url", default=cfg_base_url, env_var="BYCALL_LLM_BASE_URL", session=session
    )
    api_key = get_setting_value(
        "llm.api_key", default=cfg_api_key, env_var="BYCALL_LLM_API_KEY", session=session
    )
    model = get_setting_value(
        "llm.model", default=cfg_model, env_var="BYCALL_LLM_MODEL", session=session
    )
    embed_model = get_setting_value(
        "llm.embed_model", default=cfg_embed_model, env_var="BYCALL_LLM_EMBED_MODEL", session=session
    )
    timeout = float(
        get_setting_value(
            "llm.timeout", default=str(cfg_timeout), env_var="BYCALL_LLM_TIMEOUT", session=session
        )
        or str(cfg_timeout)
    )

    if provider == "openai":
        from bycall.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            base_url=base_url, api_key=api_key, model=model, embed_model=embed_model, timeout=timeout
        )

    if provider == "glm":
        from bycall.llm.glm import GLMClient

        return GLMClient(
            api_key=api_key, base_url=base_url, model=model, embed_model=embed_model, timeout=timeout
        )

    raise ValueError(f"不支持的 LLM provider: {provider!r}")


__all__ = ["create_llm_client"]
