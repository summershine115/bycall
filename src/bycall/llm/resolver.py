"""Model 实体解析器：DB 激活实体 → ChatOpenAI / EmbedModel。

统一"哪个是当前激活模型"的查询，供工厂（``factory.py``）和调用方复用。
admin 在控制台改激活模型后，下次查询即时生效（DB 即真值，无需重启）。

解析策略（参考 WeKnora）：所有 provider（openai/zhipu/aliyun/deepseek/
siliconflow/moonshot/ollama/generic）都提供 OpenAI 兼容端点。

- **对话模型**（M3）：用 ``langchain-openai`` 的 ``ChatOpenAI``（``BaseChatModel``，
  原生 ``bind_tools`` 供 LangGraph ``ToolNode`` 消费）。自研 chat 抽象已移除（见
  ADR-0002 + base.py docstring）。
- **向量化模型**：继续用自研 ``OpenAIEmbedModel``（RAG 入库/检索，独立于对话）。

``Model.provider`` 字段只用于 UI 展示默认 URL（见 ``llm/providers.py``），不参与分发。
未来若出现非兼容协议的 provider，在此处按 ``model.provider`` 增加分支即可。
"""

from __future__ import annotations

import time

from langchain_core.language_models import BaseChatModel
from sqlmodel import Session

from bycall.governance.models import Model
from bycall.llm.base import EmbedModel, RankResult, RerankModel
from bycall.llm.openai_compat import OpenAIEmbedModel
from bycall.observability.metrics import LLM_CALL_DURATION, LLM_CALLS

# ============ 实体查询 ============


def get_active_model(session: Session, model_type: str) -> Model | None:
    """查某类型的当前激活 Model 实体。无则返回 None。"""
    return session.exec(Model.select_active_by_type(model_type)).first()


# ============ 实体 → 模型实例 ============


class _MeteredEmbedModel(EmbedModel):
    """包装 EmbedModel：embed 调用记 RED metric（on-call Q4：外部依赖调用频率/失败/耗时）。"""

    def __init__(self, inner: EmbedModel) -> None:
        self._inner = inner

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        status = "error"
        try:
            result = self._inner.embed(texts)
            status = "ok"
            return result
        finally:
            LLM_CALLS.labels(kind="embed", status=status).inc()
            LLM_CALL_DURATION.labels(kind="embed").observe(time.perf_counter() - start)


class _MeteredRerankModel(RerankModel):
    """包装 RerankModel：rerank 调用记 RED metric。"""

    def __init__(self, inner: RerankModel) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RankResult]:
        start = time.perf_counter()
        status = "error"
        try:
            result = self._inner.rerank(query, documents, top_n=top_n)
            status = "ok"
            return result
        finally:
            LLM_CALLS.labels(kind="rerank", status=status).inc()
            LLM_CALL_DURATION.labels(kind="rerank").observe(time.perf_counter() - start)


def resolve_chat_model(
    session: Session,  # 保留入参便于未来按 session/provider 分发
    model: Model,
    *,
    timeout: float = 60.0,
) -> BaseChatModel:
    """Model 实体 → ChatOpenAI 实例（BaseChatModel）。

    M3 起对话模型统一用 ``langchain-openai`` 的 ``ChatOpenAI``：原生支持
    ``bind_tools``（function calling），供 LangGraph ``ToolNode`` 消费。
    ``base_url`` 可指向任意 OpenAI 兼容端点，覆盖现有全部 provider。

    返回 ``BaseChatModel`` 而非自研 ``ChatModel``——``ChatOpenAI`` 的调用契约是
    ``.invoke(messages) -> AIMessage``（含 ``content`` + ``tool_calls``），
    不再是自研 ``chat(messages) -> str``。
    """
    from langchain_openai import ChatOpenAI

    from bycall.governance.secrets import decrypt_secret

    return ChatOpenAI(
        base_url=model.base_url,
        api_key=decrypt_secret(model.api_key),
        model=model.name,
        timeout=timeout,
    )


def resolve_embed_model(
    session: Session,  # 保留入参便于未来按 session/provider 分发
    model: Model,
    *,
    timeout: float = 60.0,
) -> EmbedModel:
    """Model 实体 → EmbedModel 实例。

    向量化继续用自研 ``OpenAIEmbedModel``（RAG 入库/检索，独立于对话链路）。
    ``embed_dimension`` 用于向量库 schema 对齐与切换模型时的维度不匹配检测。
    """
    from bycall.governance.secrets import decrypt_secret

    return _MeteredEmbedModel(
        OpenAIEmbedModel(
            base_url=model.base_url,
            api_key=decrypt_secret(model.api_key),
            model=model.name,
            dimension=model.embed_dimension,
            timeout=timeout,
        )
    )


# ============ 便利：激活实体 + 实例一步到位 ============


def get_chat_model(
    session: Session, *, timeout: float = 60.0
) -> tuple[Model, BaseChatModel] | None:
    """查激活 chat model 并构造实例。无激活 chat 模型返回 None。"""
    entity = get_active_model(session, "chat")
    if entity is None:
        return None
    return entity, resolve_chat_model(session, entity, timeout=timeout)


def resolve_rerank_model(
    session: Session,
    entity: Model,
    *,
    timeout: float = 30.0,
) -> RerankModel:
    """Model 实体 → RerankModel 实例（OpenAI 兼容 rerank API）。"""
    from bycall.governance.secrets import decrypt_secret
    from bycall.llm.rerank import OpenAIRerankModel

    api_key = decrypt_secret(entity.api_key) if entity.api_key else ""
    # 从 SystemSetting 或 entity.base_url 取 base_url
    base_url = _resolve_base_url(session, entity)

    return _MeteredRerankModel(
        OpenAIRerankModel(
            model_name=entity.name,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
    )


def get_rerank_model(
    session: Session, *, timeout: float = 30.0
) -> tuple[Model, RerankModel] | None:
    """查激活 rerank model 并构造实例。无激活 rerank 模型返回 None。"""
    entity = get_active_model(session, "rerank")
    if entity is None:
        return None
    return entity, resolve_rerank_model(session, entity, timeout=timeout)


def _resolve_base_url(session: Session, entity: Model) -> str:
    """解析 base_url：DB 实体优先 > provider 默认 > SystemSetting override。"""
    if entity.base_url:
        return entity.base_url
    # 尝试从 provider 注册表取默认 URL
    try:
        from bycall.llm.providers import get_provider

        provider = get_provider(entity.provider)
        if provider and provider.default_base_url:
            return provider.default_base_url
    except Exception:
        pass
    return ""


def get_embed_model(session: Session, *, timeout: float = 60.0) -> tuple[Model, EmbedModel] | None:
    """查激活 embedding model 并构造实例。无激活 embedding 模型返回 None。"""
    entity = get_active_model(session, "embedding")
    if entity is None:
        return None
    return entity, resolve_embed_model(session, entity, timeout=timeout)


__all__ = [
    "get_active_model",
    "get_chat_model",
    "get_embed_model",
    "get_rerank_model",
    "resolve_chat_model",
    "resolve_embed_model",
    "resolve_rerank_model",
]
