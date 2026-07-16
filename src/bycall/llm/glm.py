"""GLM（智谱）兼容层。

拆分 ``LLMClient`` 后，GLM 不再需要独立实现——智谱 BigModel 走 OpenAI 兼容协议，
provider 注册表（``llm/providers.py``）已维护智谱默认 URL，``resolver`` 对所有
provider 统一分发到 ``OpenAIChatModel`` / ``OpenAIEmbedModel``。

本模块仅保留 ``GLM_DEFAULT_BASE_URL`` 和 ``GLMClient`` 作为薄兼容层：
- ``GLM_DEFAULT_BASE_URL``：智谱 BigModel OpenAI 兼容端点常量。
- ``GLMChatModel``：``OpenAIChatModel`` + 智谱默认 URL，供旧调用点平滑迁移。

⚠️ Deprecated（M3，见 ADR-0002）：对话模型已统一切到 ``ChatOpenAI``。
``GLMClient`` 保留代码不删，仅给历史 import 一个不崩的入口。

新代码请直接用 ``resolver`` 或 ``ChatOpenAI``，不要依赖本模块。
"""

from __future__ import annotations

from bycall.llm.openai_compat import OpenAIChatModel

# 智谱 BigModel OpenAI 兼容端点。
GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
# GLM 默认对话 / 向量模型（可被 Model.name 覆盖）。
GLM_DEFAULT_MODEL = "glm-4-flash"
GLM_DEFAULT_EMBED_MODEL = "embedding-3"


class GLMClient(OpenAIChatModel):
    """兼容层：``OpenAIChatModel`` + 智谱默认 base_url。

    注意：拆分后此类只承担"对话"职责，不再含 ``embed()``。旧的复合 ``GLMClient``
    已废弃；需要向量化的调用方请单独用 ``OpenAIEmbedModel`` 或 ``create_embed_model``。
    保留此类仅为给历史 import 一个不崩的入口。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = GLM_DEFAULT_BASE_URL,
        model: str = GLM_DEFAULT_MODEL,
        timeout: float = 60.0,
        # 兼容旧签名：embed_model 现已无用，静默忽略（避免旧调用点 TypeError）。
        embed_model: str | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, model=model, timeout=timeout)


# 别名：语义更贴合"对话模型"的新命名。
GLMChatModel = GLMClient


__all__ = [
    "GLM_DEFAULT_BASE_URL",
    "GLM_DEFAULT_EMBED_MODEL",
    "GLM_DEFAULT_MODEL",
    "GLMChatModel",
    "GLMClient",
]
