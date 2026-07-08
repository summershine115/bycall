"""LLM 客户端工厂：按配置实例化 provider。

新增 provider（Ollama / Anthropic / ...）：实现 ``LLMClient``，在此注册分支。
"""

from __future__ import annotations

from bycall.config import LLMConfig
from bycall.llm.base import LLMClient


def create_llm_client(config: LLMConfig) -> LLMClient:
    """按 ``config.provider`` 实例化一个 LLMClient。

    API key 从 config 读取（config 用 SecretStr，``get_secret_value()`` 取明文）。
    """
    provider = config.provider
    api_key = config.api_key.get_secret_value()

    if provider == "openai":
        from bycall.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            embed_model=config.embed_model,
            timeout=config.timeout,
        )

    if provider == "glm":
        from bycall.llm.glm import GLMClient

        # GLM 复用 OpenAI 兼容协议；config.base_url 默认指向智谱端点（见 glm.py）。
        # 若用户在 config 里显式改了 base_url，则尊重用户设置。
        return GLMClient(
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
            embed_model=config.embed_model,
            timeout=config.timeout,
        )

    raise ValueError(f"不支持的 LLM provider: {provider!r}")


__all__ = ["create_llm_client"]
