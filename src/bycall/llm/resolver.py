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

from bycall.governance.models import Conversation, Model
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
    from bycall.observability.usage import LLMUsageCallbackHandler

    return ChatOpenAI(
        base_url=model.base_url,
        api_key=decrypt_secret(model.api_key),
        model=model.name,
        timeout=timeout,
        callbacks=[LLMUsageCallbackHandler()],
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


def get_chat_model_by_id(
    session: Session, model_id: int, *, timeout: float = 60.0
) -> tuple[Model, BaseChatModel] | None:
    """按 id 查 chat Model 并构造实例。不存在或非 chat 类型返回 None。"""
    entity = session.get(Model, model_id)
    if entity is None or entity.model_type != "chat":
        return None
    return entity, resolve_chat_model(session, entity, timeout=timeout)


def get_chat_model_for_conversation(
    session: Session, conversation: Conversation | None, *, timeout: float = 60.0
) -> tuple[Model, BaseChatModel] | None:
    """per-session 模型优先，空/失效则 fallback 全局激活。

    ``Conversation.model_id`` 非空 → 按 id 解析；模型已删 / 类型不符 → fallback。
    T2.4 灰度：model canary 期间，无 per-session 绑定的会话按会话稳定哈希分流到候选
    模型（``resolve_canary_chat_model``），未命中或候选失效再回全局激活。
    """
    if conversation is not None and conversation.model_id is not None:
        resolved = get_chat_model_by_id(session, conversation.model_id, timeout=timeout)
        if resolved is not None:
            return resolved
    # T2.4 模型灰度：只在有会话归属时分流（无会话的单轮调用仍走全局激活）。
    if conversation is not None and conversation.id is not None:
        from bycall.governance.release import resolve_canary_chat_model

        candidate = resolve_canary_chat_model(session, conversation.id)
        if candidate is not None:
            return candidate, resolve_chat_model(session, candidate, timeout=timeout)
    # fallback 全局激活：不传 timeout，保持与既有测试 patch 的 ``lambda session:`` 签名兼容。
    return get_chat_model(session)


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
