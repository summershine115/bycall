"""GLM（智谱）LLM 客户端。

智谱 BigModel 提供 OpenAI 兼容端点（``https://open.bigmodel.cn/api/paas/v4``），
因此 GLM 直接复用 ``OpenAICompatClient``，仅默认 base_url / model 不同。

如果未来需要走智谱非兼容的原生 API（如 thoughts 模式），再在此处扩展。
"""

from __future__ import annotations

from bycall.llm.openai_compat import OpenAICompatClient

# 智谱 BigModel OpenAI 兼容端点。
GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
# GLM 默认对话 / 向量模型（可被 config.llm.model 覆盖）。
GLM_DEFAULT_MODEL = "glm-4-flash"
GLM_DEFAULT_EMBED_MODEL = "embedding-3"


class GLMClient(OpenAICompatClient):
    """GLM 客户端 = OpenAI 兼容客户端 + 智谱默认参数。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = GLM_DEFAULT_BASE_URL,
        model: str = GLM_DEFAULT_MODEL,
        embed_model: str = GLM_DEFAULT_EMBED_MODEL,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            embed_model=embed_model,
            timeout=timeout,
        )


__all__ = ["GLM_DEFAULT_BASE_URL", "GLMClient"]
