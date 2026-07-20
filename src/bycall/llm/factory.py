"""LLM 工厂入口：从 DB 激活实体构造对话 / 向量化模型。

两条数据来源走 ``resolver``，admin 控制面改激活模型后即时生效（DB 即真值，无需重启）：

- ``create_chat_model(session)`` → ``BaseChatModel``（即 ``ChatOpenAI``，M3 起）。
- ``create_embed_model(session)`` → ``EmbedModel``（自研 ``OpenAIEmbedModel``）。

这与 ``governance/models.py`` 的 ``Model`` 表对齐，参考 WeKnora。
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from sqlmodel import Session

from bycall.llm.base import EmbedModel


def create_chat_model(session: Session, *, timeout: float = 60.0) -> BaseChatModel | None:
    """从 DB 激活的 chat 模型构造 ChatOpenAI（BaseChatModel）。无激活 chat 模型返回 None。

    admin 在控制面改激活模型后，下次调用即时生效。
    """
    from bycall.llm.resolver import get_chat_model

    pair = get_chat_model(session, timeout=timeout)
    if pair is None:
        return None
    _entity, model = pair
    return model


def create_embed_model(session: Session, *, timeout: float = 60.0) -> EmbedModel | None:
    """从 DB 激活的 embedding 模型构造 EmbedModel。无激活 embedding 模型返回 None。"""
    from bycall.llm.resolver import get_embed_model

    pair = get_embed_model(session, timeout=timeout)
    if pair is None:
        return None
    _entity, model = pair
    return model


__all__ = ["create_chat_model", "create_embed_model"]
