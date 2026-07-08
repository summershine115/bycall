"""OpenAI 兼容 LLM 客户端。

基于官方 ``openai`` SDK，``base_url`` 可指向：
- OpenAI 官方：``https://api.openai.com/v1``
- 代理 / 网关：任意兼容地址
- 本地推理服务：vLLM / Ollama 的 OpenAI 兼容端点

GLM（智谱）也提供 OpenAI 兼容端点，``glm.py`` 直接复用本类，只换默认 base_url。

⚠️ M0 不强制真实 API key（冒烟测试用 mock）。生产用前需在 .env 配好 key。
"""

from __future__ import annotations

from openai import OpenAI

from bycall.llm.base import LLMClient, Message


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容协议客户端。一个实例 = 一组 base_url + key + model。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        embed_model: str,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._embed_model = embed_model
        # 显式持有 client，便于测试注入 mock / 录制。
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    def chat(self, messages: list[Message], *, temperature: float = 0.7) -> str:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=payload,
            temperature=temperature,
        )
        # 取首条 assistant 回复；空 choices 视为空回复而非报错（鲁棒性）。
        return resp.choices[0].message.content or "" if resp.choices else ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._embed_model, input=texts)
        # 按 index 对齐，避免返回顺序不稳。
        return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]

    # ---- 测试友好：允许替换底层 client（mock 注入）----

    def _set_client(self, client: object) -> None:
        """仅供测试注入 mock client。"""
        self._client = client  # type: ignore[assignment]


__all__ = ["OpenAICompatClient"]
