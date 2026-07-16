"""OpenAI 兼容向量化模型实现。

基于官方 ``openai`` SDK，``base_url`` 可指向：
- OpenAI 官方：``https://api.openai.com/v1``
- 代理 / 网关：任意兼容地址
- 本地推理服务：vLLM / Ollama 的 OpenAI 兼容端点

智谱 GLM / 阿里通义 / DeepSeek 等 Provider 都提供 OpenAI 兼容端点，因此
``resolver`` 对所有 provider 统一用本模块的 ``OpenAIEmbedModel`` 分发，provider
字段只用于 UI 展示默认 URL（见 ``llm/providers.py``），不参与分发逻辑。

对话模型（M3）已统一切到 ``langchain-openai`` 的 ``ChatOpenAI``（原生 ``bind_tools``，
供 LangGraph ``ToolNode`` 消费），不再在本模块实现——见 ADR-0002。

⚠️ M0 不强制真实 API key（冒烟测试用 mock）。生产用前需在 DB 配好 key。
"""

from __future__ import annotations

from openai import OpenAI

from bycall.llm.base import EmbedModel


class OpenAIEmbedModel(EmbedModel):
    """OpenAI 兼容向量化模型。一个实例 = 一组 base_url + key + model + dimension。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._dimension = dimension
        # 显式持有 client，便于测试注入 mock / 录制。
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    @property
    def dimension(self) -> int:
        """输出向量维度（构造时传入，供向量库 schema 对齐）。"""
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        # 按 index 对齐，避免返回顺序不稳。
        return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]

    # ---- 测试友好：允许替换底层 client（mock 注入）----

    def _set_client(self, client: object) -> None:
        """仅供测试注入 mock client。"""
        self._client = client  # type: ignore[assignment]


__all__ = ["OpenAIEmbedModel"]
