"""Agent 运行核心 —— 治理链路的单一真相源。

控制面（/chat REST）和数据面（IM 渠道）共用这一套逻辑，差异只在"回复方式"：
- /chat：把结果塞进 HTTP response
- IM：把结果通过 ReplySender 流式/单条发回

治理链路（每一步在 LLM 之前完成判定）：
  用户 → 能力集(策略引擎) → RAG(ACL预过滤) → LLM(控制面提示词+会话历史) → 审计
LLM 拿不到它无权拿的东西——策略活在代码层。

会话状态层（参考 Hermes Agent）：
- ``conversation_id`` 传入时启用多轮：加载历史 + summary → 拼 messages → 存新消息 → 压缩。
- ``conversation_id`` 为 None 时退化为单轮（兼容 M1 行为）。

参考：docs/product-design.md 四支柱、src/bycall/api/dataplane.py 原始实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session

from bycall.agent.session_store import (
    build_conversation_messages,
    compress_history,
    load_message_history,
    load_user_memory,
    save_message,
    should_compress,
)
from bycall.governance.capability import CapabilitySet, compute_capability_set
from bycall.governance.models import AuditLog, Conversation, KnowledgeBase, User
from bycall.llm import LLMClient
from bycall.rag.retrieve import build_rag_messages

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
    channel: str = "api",
    conversation_id: int | None = None,
) -> AgentResult:
    """执行一次完整的治理链路对话。

    这是治理脊柱的同步实现：身份 → 能力集 → ACL预过滤RAG → LLM → 审计。
    IM 渠道和 REST /chat 都调它，差异仅在如何拿到 user 和如何回复。

    RAG 检索（参考 WeKnora per-KB collection）：
    查用户可见的全部 KB → 每个 KB 的 collection 内按 source ACL 预过滤检索 → 合并。
    不再接收 vector_store 参数——runner 自己查 KB 路由。

    会话状态（参考 Hermes Agent）：
    - ``conversation_id`` 传入时启用多轮：加载历史 + summary → 拼 messages → 存消息 → 压缩。
    - ``conversation_id`` 为 None 时退化为单轮（兼容 M1 无状态行为）。
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

    # 2. RAG 检索（ACL 预过滤）：跨用户可见的全部 KB collection 检索。
    retrieved = _retrieve_across_kbs(session, question, cap.knowledge_scope_tags)

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

    # 3. 拼消息 + LLM 调用。
    if conversation_id is not None:
        # 多轮模式：加载历史 + summary + 用户记忆 → 拼完整 messages。
        conv = session.get(Conversation, conversation_id)
        summary = conv.summary if conv is not None else ""
        history = load_message_history(session, conversation_id)
        user_memories = load_user_memory(session, user.id) if user.id is not None else []
        rag_context = "\n\n".join(f"[{r.fields.get('source', '?')}] {r.text}" for r in retrieved)
        messages = build_conversation_messages(
            system_prompt=cap.system_prompt,
            rag_context=rag_context,
            question=question,
            history=history,
            summary=summary,
            user_memories=user_memories,
        )
    else:
        # 单轮模式（兼容 M1）：system + RAG + user，无历史。
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

    # 4. 会话持久化：存 user + assistant 消息 + 压缩检查。
    if conversation_id is not None:
        save_message(session, conversation_id, "user", question)
        save_message(session, conversation_id, "assistant", answer)

        # 压缩检查：历史 + 新消息超阈值时压缩旧消息为 summary。
        full_history = load_message_history(session, conversation_id)
        if should_compress(full_history):
            conv = session.get(Conversation, conversation_id)
            if conv is not None:
                new_summary = compress_history(
                    llm=llm,
                    history=full_history,
                    previous_summary=conv.summary,
                )
                if new_summary != conv.summary:
                    conv.summary = new_summary
                    session.add(conv)
                    logger.info("会话 %s 压缩完成，summary 已更新", conversation_id)

    # 5. 审计落库。
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


def _retrieve_across_kbs(session: Session, question: str, knowledge_scope_tags: list[str]) -> list:
    """跨用户可见的全部 KB collection 检索（参考 WeKnora per-KB collection）。

    查全部 KB → 每个 KB 的 collection 用自己的维度 open → 按 source ACL 预过滤
    检索 → 合并结果按 score 排序取 topk。

    知识域为空（无权限）→ 不检索（默认隔离）。
    admin（tags=['*']）→ 搜全部 KB（source 不预过滤）。
    """
    if not knowledge_scope_tags:
        return []

    from bycall.config import get_settings
    from bycall.rag.retrieve import retrieve_multi_kb

    # 查全部 KB（当前单租户，所有 KB 对所有用户可见，ACL 在 collection 内按 source 过滤）
    kbs = list(session.exec(KnowledgeBase.select_all()).all())
    if not kbs:
        return []

    kb_specs = [(kb.id, kb.collection_name, kb.dimension) for kb in kbs]

    return retrieve_multi_kb(
        config=get_settings().vector_store,
        kb_specs=kb_specs,
        question=question,
        knowledge_scope_tags=knowledge_scope_tags,
    )


__all__ = ["AgentResult", "run_agent"]
