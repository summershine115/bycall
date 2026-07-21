"""模型抽象基类：对话与向量化解耦为两个独立实体。

参考 WeKnora：对话与向量化是两个独立接口，可各自挂不同 provider（如 chat 用智谱
GLM、embed 用本地 Ollama）。

- **对话模型**（M3）：统一切到 ``langchain-openai`` 的 ``ChatOpenAI``
  （``BaseChatModel``，原生 ``bind_tools`` 供 LangGraph ``ToolNode`` 消费）。不再在本
  包定义自研 chat 抽象——见 ADR-0002。
- **向量化模型**：``EmbedModel``（本模块）仍是自研抽象，``OpenAIEmbedModel`` 是唯一
  实现（RAG 入库 / 检索，独立于对话链路）。
- **``Message``**：prompt 拼装的中间数据结构（runner / session_store / retrieve 用），
  最终由 runner 转成 LangChain 消息类型。它不是对话模型的 I/O 契约（对话契约是
  LangChain ``BaseMessage``）。

异步接口（``aembed``）暂以同步实现兜底；M1 RAG 链路如需高并发再换原生 async client。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Message:
    """对话消息。role 与 OpenAI 惯例一致：system / user / assistant / tool。

    用作 prompt 拼装的中间数据结构（runner / session_store / retrieve），最终由 runner
    转成 LangChain ``BaseMessage`` 子类。不是对话模型的 I/O 契约。
    """

    role: str
    content: str


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


@dataclass
class RankResult:
    """Rerank 单条结果——文档片段的相关性分数。"""

    index: int  # 片段在原列表中的索引
    score: float  # 相关性分数（0~1，越高越相关）
    text: str = ""  # 片段文本（可选，回显用）


class RerankModel(ABC):
    """重排序模型接口（provider 无关，参考 WeKnora Reranker 接口）。

    ``rerank`` 输入 query + 文档列表，返回按相关性降序排列的结果。
    用于混合检索后对 TopK 候选做精排（cross-encoder 或 API reranker）。
    """

    @abstractmethod
    def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[RankResult]:
        """对文档列表做重排序。返回按 score 降序排列的结果。

        Args:
            query: 查询文本。
            documents: 候选文档文本列表。
            top_n: 返回前 N 条（None = 全部）。

        Returns:
            按相关性降序排列的 RankResult 列表。
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """模型名称（用于日志/审计）。"""


__all__ = ["EmbedModel", "Message", "RankResult", "RerankModel"]
