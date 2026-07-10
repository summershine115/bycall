"""Agent 运行核心 —— 治理链路的单一真相源。

控制面（/chat REST）和数据面（IM 渠道）共用这一套逻辑，差异只在"回复方式"：
- /chat：把结果塞进 HTTP response
- IM：把结果通过 ReplySender 流式/单条发回

治理链路（每一步在 LLM 之前完成判定）：
  用户 → 能力集(策略引擎) → RAG(ACL预过滤) → LLM(控制面提示词) → 审计
LLM 拿不到它无权拿的东西——策略活在代码层。

参考：docs/product-design.md 四支柱、src/bycall/api/dataplane.py 原始实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session

from bycall.governance.capability import CapabilitySet, compute_capability_set
from bycall.governance.models import AuditLog, User
from bycall.llm import LLMClient
from bycall.rag.retrieve import build_rag_messages, retrieve_context
from bycall.vector_store.base import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Agent 处理结果。控制面/数据面共享。"""

    answer: str
    decision: str  # answered / denied_no_access / denied / error
    knowledge_scope: list[str]
    retrieved_chunks: int
    user_name: str
    role: str


def run_agent(
    *,
    session: Session,
    user: User,
    question: str,
    llm: LLMClient,
    vector_store: VectorStore | None,
    channel: str = "api",
) -> AgentResult:
    """执行一次完整的治理链路对话。

    这是治理脊柱的同步实现：身份 → 能力集 → ACL预过滤RAG → LLM → 审计。
    IM 渠道和 REST /chat 都调它，差异仅在如何拿到 user 和如何回复。
    """
    # 1. 策略引擎：计算能力集（禁用用户/未定义角色在此被拒）。
    try:
        cap: CapabilitySet = compute_capability_set(session, user)
    except PermissionError as e:
        _audit(session, user, channel, question, "denied", str(e), "")
        return _result(
            answer=f"权限不足：{e}",
            decision="denied",
            scope=[],
            retrieved=0,
            user=user,
        )

    # 2. RAG 检索（ACL 预过滤）：只召回 knowledge_scope 内的 chunk。
    retrieved = []
    if vector_store is not None:
        retrieved = retrieve_context(
            store=vector_store,
            question=question,
            knowledge_scope_tags=cap.knowledge_scope_tags,
        )

    # 无任何可见知识域 → 不进 LLM，直接拒绝（不让 LLM 看到问题）。
    has_access = ("*" in cap.knowledge_scope_tags) or bool(cap.knowledge_scope_tags)
    if not has_access:
        _audit(session, user, channel, question, "denied_no_access", "无可见知识域", "")
        return _result(
            answer="你没有访问相关知识的权限，请联系管理员。",
            decision="denied_no_access",
            scope=cap.knowledge_scope_tags,
            retrieved=0,
            user=user,
        )

    # 3. 拼 prompt + LLM 调用。
    messages = build_rag_messages(cap.system_prompt, question, retrieved)
    try:
        answer = llm.chat(messages)
    except Exception as e:
        logger.error("LLM 调用失败: %s", e)
        _audit(session, user, channel, question, "error", str(e)[:200], "")
        return _result(
            answer="抱歉，服务暂时不可用。",
            decision="error",
            scope=cap.knowledge_scope_tags,
            retrieved=len(retrieved),
            user=user,
        )

    # 4. 审计落库。
    _audit(
        session,
        user,
        channel,
        question,
        "answered",
        f"retrieved={len(retrieved)} chunks, scope={cap.knowledge_scope_tags}",
        answer[:500],
    )

    return _result(
        answer=answer,
        decision="answered",
        scope=cap.knowledge_scope_tags,
        retrieved=len(retrieved),
        user=user,
    )


def _result(
    *, answer: str, decision: str, scope: list[str], retrieved: int, user: User
) -> AgentResult:
    return AgentResult(
        answer=answer,
        decision=decision,
        knowledge_scope=list(scope),
        retrieved_chunks=retrieved,
        user_name=user.name,
        role=user.role,
    )


def _audit(
    session: Session,
    user: User,
    channel: str,
    question: str,
    decision: str,
    detail: str,
    answer: str,
) -> None:
    """写一条审计日志（append-only）。"""
    session.add(
        AuditLog(
            user_id=user.id,
            user_name=user.name,
            channel=channel,
            action="chat",
            question=question[:500],
            decision=decision,
            detail=detail,
            answer=answer[:500],
        )
    )
    session.commit()


__all__ = ["AgentResult", "run_agent"]
