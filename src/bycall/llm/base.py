"""模型抽象基类：对话与向量化解耦为两个独立实体。

参考 WeKnora：``ChatModel``（对话）与 ``EmbedModel``（向量化）是两个独立接口，
可各自挂不同 provider（如 chat 用智谱 GLM、embed 用本地 Ollama）。
M0 把它们绑在同一个 ``LLMClient``（chat+embed 合一）是历史遗留；现拆开。

为兼容现有调用方（``runner`` / ``ingest`` / ``dataplane`` 等），保留 ``LLMClient``
作为 ``ChatModel`` + ``EmbedModel`` 的复合接口——它仍然同时具备 ``chat()`` 与
``embed()``，所以旧代码与 ``MagicMock(spec=LLMClient)`` 不受影响。新代码应直接
依赖 ``ChatModel`` / ``EmbedModel``，按需注入。

异步接口（``achat`` / ``aembed``）暂以同步实现兜底；M1 RAG 链路如需高并发再换
原生 async client。接口先留好，避免后续破坏性改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Message:
    """对话消息。role 与 OpenAI 惯例一致：system / user / assistant / tool。"""

    role: str
    content: str


class ChatModel(ABC):
    """对话模型接口（provider 无关）。"""

    @abstractmethod
    def chat(self, messages: list[Message], *, temperature: float = 0.7) -> str:
        """文本对话。返回 assistant 回复内容（纯文本）。"""

    async def achat(self, messages: list[Message], *, temperature: float = 0.7) -> str:
        """异步对话。M0 默认同步实现兜底，M1 按需换原生 async。"""
        return self.chat(messages, temperature=temperature)


class EmbedModel(ABC):
    """向量化模型接口（provider 无关）。

    ``dimension`` 暴露向量维度，供向量库 collection schema 对齐与切换模型时的
    维度不匹配检测。
    """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """输出向量维度。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。返回与输入等长的向量列表。

        用于 RAG 入库（文档切块）与查询（问题向量化）。
        """

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        """异步向量化。M0 默认同步实现兜底，M1 按需换原生 async。"""
        return self.embed(texts)


class LLMClient(ChatModel, EmbedModel):
    """复合兼容接口：chat + embed 合一（历史遗留形态）。

    新代码不要继承/依赖它——按需用 ``ChatModel`` 或 ``EmbedModel``。保留它只为
    不破坏现有调用点（``runner``/``ingest``/``dataplane``）与测试里的
    ``MagicMock(spec=LLMClient)``（后者需要同时 spec 出 chat 与 embed）。

    子类需同时实现 ``chat()``、``embed()`` 与 ``dimension``。
    """

    @property
    @abstractmethod
    def dimension(self) -> int: ...


__all__ = ["ChatModel", "EmbedModel", "LLMClient", "Message"]
