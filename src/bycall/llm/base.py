"""LLM 客户端抽象基类。

业务层只依赖 ``LLMClient``，不感知具体 provider（openai/glm/...）。
M0 提供 ``chat()``（文本对话）与 ``embed()``（向量化，供 RAG 用）两个核心能力。

异步接口（``achat`` / ``aembed``）暂以同步实现兜底；M1 RAG 链路如需高并发
再换原生 async client。接口先留好，避免后续破坏性改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Message:
    """对话消息。role 与 OpenAI 惯例一致：system / user / assistant / tool。"""

    role: str
    content: str


class LLMClient(ABC):
    """模型无关 LLM 客户端。"""

    @abstractmethod
    def chat(self, messages: list[Message], *, temperature: float = 0.7) -> str:
        """文本对话。返回 assistant 回复内容（纯文本）。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。返回与输入等长的向量列表，每条维度由 embed_model 决定。

        用于 RAG 入库（文档切块）与查询（问题向量化）。
        """

    # ---- 异步接口（M0 同步兜底，M1 按需换原生 async）----

    async def achat(self, messages: list[Message], *, temperature: float = 0.7) -> str:
        """异步对话。M0 默认同步实现。"""
        return self.chat(messages, temperature=temperature)

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """异步向量化。M0 默认同步实现。"""
        return self.embed(texts)
