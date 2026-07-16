"""LLM 工厂入口：从配置构造 ChatModel / EmbedModel。

两条数据来源：
1. **DB 激活实体（推荐，新代码入口）**：``create_chat_model(session)`` /
   ``create_embed_model(session)`` 走 ``resolver``，admin 控制面改激活模型后即时生效。
   这与 ``governance/models.py`` 的 ``Model`` 表对齐，参考 WeKnora。
2. **传入 config（兼容旧路径）**：``create_llm_client(config)`` 仍按 ``LLMConfig``
   构造，供未接入 DB 的旧调用点（如纯单元测试、IM handler 早期初始化）兜底。

M3 变更（见 ADR-0002）：对话模型统一切到 ``langchain-openai`` 的 ``ChatOpenAI``
（原生 ``bind_tools``，供 LangGraph ``ToolNode`` 消费）。``create_chat_model`` /
``create_llm_client`` 返回 ``BaseChatModel``；``create_embed_model`` 仍返回自研
``EmbedModel``（向量化独立于对话链路）。

兼容说明：
- ``create_llm_client`` 保留旧签名，内部优先走 DB（若 ``session`` 可用且 DB 有激活
  chat 模型），否则回退到 config 构造。返回 ``BaseChatModel``。
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from sqlmodel import Session

from bycall.llm.base import EmbedModel

# ============ 新入口：DB 激活实体驱动 ============


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


# ============ 兼容入口：config 驱动 ============


def create_llm_client(config=None, *, session: Session | None = None) -> BaseChatModel:
    """兼容入口：返回一个对话模型（``BaseChatModel``，即 ``ChatOpenAI``）。

    优先级：DB 激活 chat 实体（``session`` 可用且 DB 有数据）> config 兜底。
    admin 改 DB 后下一条消息即时生效。
    """
    # 1. 优先：DB 激活实体
    if session is not None:
        chat = create_chat_model(session)
        if chat is not None:
            return chat

    # 2. 兜底：从 config（.env / LLMConfig / SimpleNamespace）构造 ChatOpenAI。
    #    用 getattr 容错（部分测试传 SimpleNamespace）。
    cfg_base_url = getattr(config, "base_url", "https://api.openai.com/v1")
    _key = getattr(config, "api_key", None)
    cfg_api_key = _key.get_secret_value() if hasattr(_key, "get_secret_value") else str(_key or "")
    cfg_model = getattr(config, "model", "gpt-4o-mini")
    cfg_timeout = float(getattr(config, "timeout", 60.0))

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=cfg_base_url,
        api_key=cfg_api_key,
        model=cfg_model,
        timeout=cfg_timeout,
    )


__all__ = ["create_chat_model", "create_embed_model", "create_llm_client"]
