"""Agent 运行核心 —— 治理链路的单一真相源。

控制面（/chat REST）和数据面（IM 渠道）共用这一套逻辑，差异只在"回复方式"：
- /chat：把结果塞进 HTTP response
- IM：把结果通过 ReplySender 流式/单条发回

治理链路（每一步在 LLM 之前完成判定）：
  用户 → 能力集(策略引擎) → RAG(ACL预过滤) → 工具网关(allowed_tools过滤) → LLM(graph回路) → 审计
LLM 拿不到它无权拿的东西——策略活在代码层。

M3 工具治理（见 ADR-0002）：
- 对话模型从自研 ``OpenAIChatModel`` 切到 LangChain ``ChatOpenAI``（原生 bind_tools）。
- 工具按 ``CapabilitySet.allowed_tools`` 过滤（``tools.gateway.filter_tools``），
  LLM 只见白名单内的工具——无权工具 LLM 根本看不到（支柱 3）。
- 工具回路由 ``agent.graph.run_tool_loop`` 执行（LangGraph StateGraph + ToolNode）。

会话状态层（参考 Hermes Agent）：
- ``conversation_id`` 传入时启用多轮：加载历史 + summary → 拼 messages → 存新消息 → 压缩。
- ``conversation_id`` 为 None 时退化为单轮（兼容 M1 行为）。

参考：docs/product-design.md 四支柱、src/bycall/api/dataplane.py 原始实现。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlmodel import Session

from bycall.agent.graph import GraphResult, run_tool_loop
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
from bycall.llm.base import Message as LLMMessage
from bycall.rag.retrieve import build_rag_messages
from bycall.tools.gateway import filter_tools, to_langchain_tools

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
    tool_calls: list[dict]  # 本次触发的工具调用记录（name, args），审计透明化


def run_agent(
    *,
    session: Session,
    user: User,
    question: str,
    llm: BaseChatModel,
    channel: str = "api",
    conversation_id: int | None = None,
) -> AgentResult:
    """执行一次完整的治理链路对话。

    这是治理脊柱的同步实现：身份 → 能力集 → ACL预过滤RAG → 工具过滤 → LLM(graph) → 审计。
    IM 渠道和 REST /chat 都调它，差异仅在如何拿到 user 和如何回复。

    RAG 检索（参考 WeKnora per-KB collection）：
    查用户可见的全部 KB → 每个 KB 的 collection 内按 source ACL 预过滤检索 → 合并。

    工具治理（M3）：
    ``CapabilitySet.allowed_tools`` → ``filter_tools`` → ``to_langchain_tools`` → graph。
    无权工具 LLM 看不到；空工具集退化为纯对话。

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

    # 3. 工具网关过滤：按 allowed_tools 裁剪工具集，LLM 只见白名单内的工具。
    allowed_tool_specs = filter_tools(cap.allowed_tools)
    langchain_tools = to_langchain_tools(allowed_tool_specs, cap)

    # 4. 拼消息（含提示词注入防御——两级隔离，见 session_store.build_conversation_messages）：
    #    - memory 进 system（admin 管控的可信数据，享 system 权重，靠"非指令"标注软隔离）。
    #    - summary / RAG 进 user（不可信：summary 来自历史，RAG 来自外部文档，硬隔离降权）。
    if conversation_id is not None:
        # 多轮模式：加载历史 + summary + 用户记忆 → 拼完整 messages。
        conv = session.get(Conversation, conversation_id)
        summary = conv.summary if conv is not None else ""
        history = load_message_history(session, conversation_id)
        user_memories = load_user_memory(session, user.id) if user.id is not None else []
        rag_context = "\n\n".join(f"[{r.fields.get('source', '?')}] {r.text}" for r in retrieved)
        raw_messages = build_conversation_messages(
            system_prompt=cap.system_prompt,
            rag_context=rag_context,
            question=question,
            history=history,
            summary=summary,
            user_memories=user_memories,
        )
    else:
        # 单轮模式（兼容 M1）：system + RAG + user，无历史。
        raw_messages = build_rag_messages(cap.system_prompt, question, retrieved)

    # 自研 LLMMessage → LangChain 消息（ChatOpenAI / graph 需要 LangChain 类型）。
    messages = _to_langchain_messages(raw_messages)

    # 5. 执行工具回路（LangGraph）：model ↔ tool 循环，直到无 tool_calls。
    try:
        graph_result: GraphResult = run_tool_loop(
            chat_model=llm,
            tools=langchain_tools,
            messages=messages,
        )
        answer = graph_result.answer
    except Exception as e:
        logger.error("LLM/graph 执行失败: %s", e)
        _audit(session, user, channel, question, "error", str(e)[:200], "")
        return _result(
            answer="抱歉，服务暂时不可用。",
            decision="error",
            scope=cap.knowledge_scope_tags,
            retrieved=len(retrieved),
            user=user,
        )

    # 6. 会话持久化：存 user + assistant 消息 + 压缩检查。
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

    # 7. 审计落库（含工具调用记录）。
    # detail 保留工具名摘要（快速扫），tool_results 存结构化明细 [{name,args,result}]
    # （result 为脱敏后值，支柱 3「调用后审计落库」+ 支柱 4 第二条泄漏面）。
    tool_detail = ""
    if graph_result.tool_calls_made:
        tool_names = [tc["name"] for tc in graph_result.tool_calls_made]
        tool_detail = f"tools={tool_names}, "
    _audit(
        session,
        user,
        channel,
        question,
        "answered",
        f"{tool_detail}retrieved={len(retrieved)} chunks, scope={cap.knowledge_scope_tags}",
        answer[:500],
        tool_results=json.dumps(graph_result.tool_calls_made, ensure_ascii=False),
    )

    return _result(
        answer=answer,
        decision="answered",
        scope=cap.knowledge_scope_tags,
        retrieved=len(retrieved),
        user=user,
        tool_calls=graph_result.tool_calls_made,
    )


def _result(
    *,
    answer: str,
    decision: str,
    scope: list[str],
    retrieved: int,
    user: User,
    tool_calls: list[dict] | None = None,
) -> AgentResult:
    return AgentResult(
        answer=answer,
        decision=decision,
        knowledge_scope=list(scope),
        retrieved_chunks=retrieved,
        user_name=user.name,
        role=user.role,
        tool_calls=tool_calls or [],
    )


def _audit(
    session: Session,
    user: User,
    channel: str,
    question: str,
    decision: str,
    detail: str,
    answer: str,
    tool_results: str = "",
) -> None:
    """写一条审计日志（append-only）。

    ``tool_results`` 仅数据面 answered 路径传（``[{name,args,result}]`` JSON，result
    脱敏后值）；denied/error 路径留空。
    """
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
            tool_results=tool_results,
        )
    )
    session.commit()


def _to_langchain_messages(messages: list[LLMMessage]) -> list[BaseMessage]:
    """自研 LLMMessage → LangChain 消息类型。

    ChatOpenAI.invoke / LangGraph graph 需要 LangChain 的 BaseMessage 子类。
    自研 LLMMessage 只有 role + content，按 role 分发到对应 LangChain 类型。
    提示词注入防御的 role 分配（system 只给控制面提示词，summary/RAG 是 user 角色）
    在 build_conversation_messages 里已定好，这里只做机械转换，不改 role 语义。
    """
    mapping = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    result: list[BaseMessage] = []
    for msg in messages:
        cls = mapping.get(msg.role, HumanMessage)
        result.append(cls(content=msg.content))
    return result


def _retrieve_across_kbs(session: Session, question: str, knowledge_scope_tags: list[str]) -> list:
    """跨用户可见的全部 KB collection 检索（参考 WeKnora per-KB collection）。

    查全部 KB → 每个 KB 用各自绑定的 embedding 模型 embed query → 该 KB 的 collection
    内做向量+FTS 混合检索（source ACL 预过滤）→ 跨 KB 用 RRF 融合取 topk。

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

    kb_specs = [(kb.id, kb.collection_name, kb.dimension, kb.embedding_model_id) for kb in kbs]

    return retrieve_multi_kb(
        session=session,
        config=get_settings().vector_store,
        kb_specs=kb_specs,
        question=question,
        knowledge_scope_tags=knowledge_scope_tags,
    )


__all__ = ["AgentResult", "run_agent"]
