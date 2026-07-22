"""OpenAI 兼容 Rerank 模型实现。

支持任意 OpenAI-compatible rerank API（Jina / Cohere / 阿里云 / 智谱 等），
只要 API 接受 ``model + query + documents`` 并返回 ``results[index, score]``。
"""

from __future__ import annotations

import logging

import httpx

from bycall.llm.base import RankResult, RerankModel

logger = logging.getLogger(__name__)


class OpenAIRerankModel(RerankModel):
    """OpenAI 兼容 rerank 模型——基于 OpenAI client 调用 rerank API。

    支持的 provider（任何兼容 OpenAI rerank 格式的 API）：
    - Jina (https://api.jina.ai/v1)
    - Cohere (https://api.cohere.ai/v1)
    - 阿里云 DashScope
    - 智谱 GLM
    - 硅基流动
    - 本地 vLLM / TEI 等

    API 格式（OpenAI 兼容）：
    POST /rerank
    {
        "model": "jina-reranker-v2-base-multilingual",
        "query": "what is the capital of France?",
        "documents": ["Paris is...", "London is...", "Berlin is..."],
        "top_n": 3
    }
    """

    def __init__(
        self,
        model_name: str,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        self._name = model_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._name

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RankResult]:
        """调用 OpenAI 兼容 rerank API 做重排序。"""
        if not documents:
            return []

        try:
            body: dict = {
                "model": self._name,
                "query": query,
                "documents": documents,
            }
            if top_n is not None:
                body["top_n"] = top_n

            # 直接用 httpx 发请求（带显式 Authorization header）。
            # 不走 OpenAI SDK 的 _client.post()——SDK 内部 httpx Client 不经过 auth 层。
            resp = httpx.post(
                f"{self._base_url}/rerank",
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            results: list[RankResult] = []
            for item in data.get("results", []):
                idx = item.get("index", 0)
                # 兼容不同 provider 的字段名：硅基流动用 relevance_score，Jina/Cohere 用 score
                score = item.get("relevance_score") or item.get("score", 0.0)
                text = documents[idx] if 0 <= idx < len(documents) else ""
                results.append(RankResult(index=idx, score=score, text=text))

            # 按 score 降序排列
            results.sort(key=lambda r: r.score, reverse=True)

            if top_n is not None:
                results = results[:top_n]

            return results

        except Exception as e:
            logger.warning("Rerank API 调用失败: %s", e)
            # 降级：返回原始顺序
            return [RankResult(index=i, score=0.0, text=doc) for i, doc in enumerate(documents)]
