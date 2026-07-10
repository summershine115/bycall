"""Model 实体解析器：DB 激活实体 → ChatModel / EmbedModel。

统一"哪个是当前激活模型"的查询，供工厂（``factory.py``）和调用方复用。
admin 在控制台改激活模型后，下次查询即时生效（DB 即真值，无需重启）。

解析策略（参考 WeKnora）：目前所有 provider（openai/zhipu/aliyun/deepseek/
siliconflow/moonshot/ollama/generic）都提供 OpenAI 兼容端点，因此统一分发到
``OpenAIChatModel`` / ``OpenAIEmbedModel``；``Model.provider`` 字段只用于 UI 展示
默认 URL（见 ``llm/providers.py``），不参与分发。未来若出现非兼容协议的 provider，
在此处按 ``model.provider`` 增加分支即可。
"""

from __future__ import annotations

from sqlmodel import Session

from bycall.governance.models import Model
from bycall.llm.base import ChatModel, EmbedModel
from bycall.llm.openai_compat import OpenAIChatModel, OpenAIEmbedModel

# ============ 实体查询 ============


def get_active_model(session: Session, model_type: str) -> Model | None:
    """查某类型的当前激活 Model 实体。无则返回 None。"""
    return session.exec(Model.select_active_by_type(model_type)).first()


# ============ 实体 → 模型实例 ============


def resolve_chat_model(
    session: Session,  # 保留入参便于未来按 session/provider 分发
    model: Model,
    *,
    timeout: float = 60.0,
) -> ChatModel:
    """Model 实体 → ChatModel 实例。

    目前所有 provider 都走 OpenAI 兼容协议，直接构造 ``OpenAIChatModel``。
    未来出现非兼容 provider 时，按 ``model.provider`` 分支扩展。
    """
    return OpenAIChatModel(
        base_url=model.base_url,
        api_key=model.api_key,
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

    目前所有 provider 都走 OpenAI 兼容协议，直接构造 ``OpenAIEmbedModel``。
    ``embed_dimension`` 用于向量库 schema 对齐与切换模型时的维度不匹配检测。
    """
    return OpenAIEmbedModel(
        base_url=model.base_url,
        api_key=model.api_key,
        model=model.name,
        dimension=model.embed_dimension,
        timeout=timeout,
    )


# ============ 便利：激活实体 + 实例一步到位 ============


def get_chat_model(session: Session, *, timeout: float = 60.0) -> tuple[Model, ChatModel] | None:
    """查激活 chat model 并构造实例。无激活 chat 模型返回 None。"""
    entity = get_active_model(session, "chat")
    if entity is None:
        return None
    return entity, resolve_chat_model(session, entity, timeout=timeout)


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
    "resolve_chat_model",
    "resolve_embed_model",
]
