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

参考：doc/product-design.md 四支柱、src/bycall/api/dataplane.py 原始实现。
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlmodel import Session, select

from bycall.agent.context_budget import budget_and_format_rag, resolve_budget
from bycall.agent.graph import TOOL_ROUND_FLUSH, GraphResult, run_tool_loop, run_tool_loop_stream
from bycall.agent.memory_writer import maybe_extract_and_save_memory
from bycall.agent.runtime_context import build_runtime_context
from bycall.agent.session_store import (
    build_context_trace,
    build_conversation_messages,
    compress_conversation,
    load_agent_memory,
    load_message_history,
    load_user_memory,
    save_message,
)
from bycall.db import session_scope
from bycall.governance.capability import CapabilitySet, compute_capability_set
from bycall.governance.models import AuditLog, Conversation, KnowledgeBase, User
from bycall.llm.base import Message as LLMMessage
from bycall.observability import get_tracer, traced
from bycall.observability.metrics import (
    LLM_CALL_DURATION,
    LLM_CALLS,
    TURN_DURATION,
    TURN_FAILURES,
    TURNS_TOTAL,
)
from bycall.rag.retrieve import RetrievalOutcome, build_rag_messages, rewrite_query
from bycall.tools.gateway import filter_tools, to_langchain_tools
from bycall.tools.registry import get_tool

logger = logging.getLogger(__name__)


# ============ 后台压缩（context engineering：压缩不阻塞请求） ============

# 测试置 True → 同步压缩（确定性好断言）；生产保持 False（后台线程）。
_COMPACTION_SYNC = False
_MEMORY_SYNC = False
_background_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-maintenance")
_background_slots = threading.BoundedSemaphore(34)
_compactor_pending: set[int] = set()
_compactor_guard = threading.Lock()
_prepare_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-prepare")
_prepare_slots = threading.BoundedSemaphore(16)
atexit.register(_background_executor.shutdown, wait=False, cancel_futures=True)
atexit.register(_prepare_executor.shutdown, wait=False, cancel_futures=True)


def _submit_maintenance(func, *args, **kwargs):
    """可选维护任务满载时跳过；限制运行中与排队总数，避免积压持有对话全文。"""
    if not _background_slots.acquire(blocking=False):
        logger.warning("后台维护队列已满，跳过 %s", func.__name__)
        return None
    try:
        future = _background_executor.submit(func, *args, **kwargs)
    except RuntimeError:
        _background_slots.release()
        logger.warning("后台维护已停止，跳过任务")
        return None
    future.add_done_callback(lambda _: _background_slots.release())
    return future


def _run_compaction(conversation_id: int) -> None:
    """后台压缩自建 session；跨进程并发由 compress_conversation 的 DB 租约控制。"""
    from bycall.llm import resolver

    try:
        with session_scope() as session:
            resolved = resolver.get_chat_model(session)
            if resolved is None:
                return
            compress_conversation(session, conversation_id, resolved[1])
    except Exception:
        logger.exception("后台压缩失败 conv=%s", conversation_id)


def schedule_compaction(conversation_id: int) -> None:
    """同会话至多一个待处理任务，结束后删除登记，不积累永久会话锁。"""
    if _COMPACTION_SYNC:
        _run_compaction(conversation_id)
        return
    with _compactor_guard:
        if conversation_id in _compactor_pending:
            return
        _compactor_pending.add(conversation_id)

    def done(_future):
        with _compactor_guard:
            _compactor_pending.discard(conversation_id)

    future = _submit_maintenance(_run_compaction, conversation_id)
    if future is None:
        done(None)
    else:
        future.add_done_callback(done)


def schedule_memory_extraction(**kwargs) -> None:
    """回答提交后再调度可选记忆提取；测试可同步执行以保证隔离。"""
    if _MEMORY_SYNC:
        maybe_extract_and_save_memory(**kwargs)
    else:
        _submit_maintenance(maybe_extract_and_save_memory, **kwargs)


_tracer = get_tracer()


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
    citations: list[dict] = field(default_factory=list)  # M6：RAG 引用元数据（来源卡片用）
    context_trace: str = ""  # 本轮上下文快照 JSON（system/RAG/记忆/摘要），排障用
    retrieval_status: str = "not_requested"
    retrieval_diagnostics: list[dict] = field(default_factory=list)


def _observe_turn(decision: str, start: float) -> None:
    """记录一轮对话的 T2.5 运营指标：延迟 / 轮次 / 失败。"""
    TURN_DURATION.observe(time.perf_counter() - start)
    TURNS_TOTAL.labels(decision=decision or "unknown").inc()
    if decision == "error":
        TURN_FAILURES.labels(kind="llm_error").inc()


@traced("bycall.run_agent")
def run_agent(
    *,
    session: Session,
    user: User,
    question: str,
    llm: BaseChatModel,
    channel: str = "api",
    conversation_id: int | None = None,
    im_user_id: str = "",
    agent_id: int | None = None,
) -> AgentResult:
    """执行一次完整的治理链路对话（观测包装）。

    实际逻辑在 ``_run_agent_impl``；本包装只做轮次计时与 T2.5 指标落账。
    """
    start = time.perf_counter()
    result = _run_agent_impl(
        session=session,
        user=user,
        question=question,
        llm=llm,
        channel=channel,
        conversation_id=conversation_id,
        im_user_id=im_user_id,
        agent_id=agent_id,
    )
    _observe_turn(result.decision, start)
    return result


def _run_agent_impl(
    *,
    session: Session,
    user: User,
    question: str,
    llm: BaseChatModel,
    channel: str = "api",
    conversation_id: int | None = None,
    im_user_id: str = "",
    agent_id: int | None = None,
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
    # 任务触发判定（S1a T5）：显式「启动任务：…」→ 走任务引擎；否则原路径（零回归）。
    task_goal = _match_task_trigger(question)
    if task_goal is not None:
        return _run_as_task(
            session=session,
            user=user,
            question=question,
            goal=task_goal,
            llm=llm,
            channel=channel,
            conversation_id=conversation_id,
        )
    # 1-4. 治理前置管线（能力集 → RAG 预过滤 → 工具过滤 → 消息 + 能力/审批令牌）。
    decision, payload = _prepare_turn(
        llm=llm,
        session=session,
        user=user,
        question=question,
        channel=channel,
        conversation_id=conversation_id,
        im_user_id=im_user_id,
        agent_id=agent_id,
    )
    if decision != "ready":
        return payload  # denied / denied_no_access（已审计）

    # 模型等待前释放前置读取事务；快照避免 commit 后自动刷新用户而重新占用连接。
    user = User.model_validate(user)
    session.commit()
    cap = payload.cap
    retrieved = payload.retrieved
    langchain_tools = payload.langchain_tools
    messages = payload.messages
    context_trace = payload.context_trace
    cap_token, approval_token = _set_turn_context(cap, user, conversation_id, channel, im_user_id)

    # 5. 执行工具回路（LangGraph）：model ↔ tool 循环，直到无 tool_calls。
    from bycall.mcp.context import reset_current_capability
    from bycall.tools.approval import reset_approval_context

    try:
        _chat_start = time.perf_counter()
        graph_result: GraphResult = run_tool_loop(
            chat_model=llm,
            tools=langchain_tools,
            messages=messages,
        )
        LLM_CALLS.labels(kind="chat", status="ok").inc()
        LLM_CALL_DURATION.labels(kind="chat").observe(time.perf_counter() - _chat_start)
        answer = graph_result.answer
    except Exception as e:
        LLM_CALLS.labels(kind="chat", status="error").inc()
        LLM_CALL_DURATION.labels(kind="chat").observe(time.perf_counter() - _chat_start)
        logger.error("LLM/graph 执行失败: %s", e)
        _audit(session, user, channel, question, "error", str(e)[:200], "")
        return _result(
            answer="抱歉，服务暂时不可用。",
            decision="error",
            scope=cap.knowledge_scope_tags,
            retrieved=len(retrieved),
            user=user,
        )
    finally:
        reset_current_capability(cap_token)
        reset_approval_context(approval_token)

    # 6-7. 会话持久化 + 审计 + 构造结果（两路共用）。
    result = _finalize_turn(
        session=session,
        user=user,
        channel=channel,
        question=question,
        cap=cap,
        retrieved=retrieved,
        graph_result=graph_result,
        answer=answer,
        context_trace=context_trace,
        conversation_id=conversation_id,
    )
    if graph_result.tool_calls_made:
        schedule_memory_extraction(llm=llm, user_id=user.id, question=question, answer=answer)
    return result


def _result(
    *,
    answer: str,
    decision: str,
    scope: list[str],
    retrieved: int,
    user: User,
    tool_calls: list[dict] | None = None,
    citations: list[dict] | None = None,
    context_trace: str = "",
) -> AgentResult:
    retrieval = json.loads(context_trace or "{}").get("retrieval", {})
    return AgentResult(
        answer=answer,
        decision=decision,
        knowledge_scope=list(scope),
        retrieved_chunks=retrieved,
        user_name=user.name,
        role=user.role,
        tool_calls=tool_calls or [],
        citations=citations or [],
        context_trace=context_trace,
        retrieval_status=retrieval.get("status", "not_requested"),
        retrieval_diagnostics=retrieval.get("issues", []),
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
    context_trace: str = "",
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
            context_trace=context_trace,
        )
    )
    session.commit()


def _prepare_agent_messages(
    *,
    session: Session,
    user: User,
    cap: CapabilitySet,
    question: str,
    channel: str,
    conversation_id: int | None,
    retrieved: list,
) -> tuple[list, list, list[BaseMessage], str]:
    """按本轮治理结果统一准备工具和消息，供同步与流式入口共用。"""
    budget = resolve_budget(cap.context_budget_overrides)
    allowed_tool_specs = filter_tools(cap.allowed_tools)
    if cap.on_demand_skills:
        loader = get_tool("load_skill")
        if loader and loader not in allowed_tool_specs:
            allowed_tool_specs.append(loader)
    langchain_tools = to_langchain_tools(allowed_tool_specs, cap)

    history = []
    summary = ""
    user_memories = (
        load_user_memory(session, user.id, question=question, budget=budget)
        if user.id is not None
        else []
    )
    # agent 自动记录记忆（不可信，user 角色硬隔离）——与 admin 记忆分开加载。
    agent_memories = (
        load_agent_memory(session, user.id, budget=budget) if user.id is not None else []
    )
    if conversation_id is not None:
        conv = session.get(Conversation, conversation_id)
        summary = conv.summary if conv is not None else ""
        history = load_message_history(session, conversation_id, budget=budget)

    runtime_context = build_runtime_context(
        user=user,
        capability=cap,
        channel=channel,
        conversation_id=conversation_id,
        tools=allowed_tool_specs,
        has_history=bool(history),
        has_summary=bool(summary),
        admin_memory_count=len(user_memories),
        agent_memory_count=len(agent_memories),
    )
    # RAG 预算化（缺陷 1）：per-chunk 截断 + 跨 KB 去重 + 低相关过滤 + 相关性标注 +
    # 投毒软降级。多轮/单轮两路共用同一预算化产物（消除旧的"裸拼接抄两份"缝隙）。
    rag_context, rag_report = budget_and_format_rag(retrieved, budget)
    retrieval = (
        retrieved.to_dict()
        if isinstance(retrieved, RetrievalOutcome)
        else {
            "status": "ok" if retrieved else "no_hit",
            "has_evidence": bool(retrieved),
            "issues": [],
        }
    )
    if retrieval["status"] in ("degraded", "error"):
        notice = (
            "知识检索暂不可用。" if retrieval["status"] == "error" else "知识检索已降级或部分失败。"
        )
        rag_context = (
            notice
            + "现有资料可能不完整；回答应说明此限制，无证据时不可声称有知识库依据。\n"
            + rag_context
        )
    elif not rag_report.evidence:
        rag_context = "没有可用的知识证据；不要编造知识库结论或引用，可说明资料不足。"
    else:
        rag_context = (
            "引用资料时使用对应 [E编号]；来源列表表示提供的证据，不代表逐句事实核验。\n"
            + rag_context
        )
    if conversation_id is not None:
        raw_messages = build_conversation_messages(
            system_prompt=cap.system_prompt,
            rag_context=rag_context,
            question=question,
            history=history,
            summary=summary,
            user_memories=user_memories,
            agent_memories=agent_memories,
            skill_markdown=cap.skill_markdown,
            knowledge_scope_text=cap.knowledge_scope_text,
            runtime_context=runtime_context,
        )
    else:
        raw_messages = build_rag_messages(
            cap.system_prompt,
            question,
            rag_context=rag_context,
            skill_markdown=cap.skill_markdown,
            knowledge_scope_text=cap.knowledge_scope_text,
            user_memories=user_memories,
            runtime_context=runtime_context,
        )
    context_trace = build_context_trace(
        raw_messages=raw_messages,
        retrieved=rag_report.evidence,
        user_memories=user_memories,
        agent_memories=agent_memories,
        summary=summary,
        history=history,
        rag_report=rag_report,
    )
    trace = json.loads(context_trace or "{}")
    trace["retrieval"] = retrieval
    trace["rag_candidates"] = len(retrieved)
    trace["rag_evidence"] = _build_citations(rag_report.evidence)
    context_trace = json.dumps(trace, ensure_ascii=False)
    return allowed_tool_specs, langchain_tools, _to_langchain_messages(raw_messages), context_trace


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


def _visible_kbs_statement(cap: CapabilitySet):
    """KB 可见性先在 SQL 收窄；查询和改写判定复用同一条件。"""
    stmt = select(KnowledgeBase).order_by(KnowledgeBase.id)
    if not cap.is_admin:
        stmt = stmt.where(KnowledgeBase.id.in_(cap.visible_kb_ids))
    return stmt


def _retrieve_across_kbs(
    session: Session,
    question: str,
    cap,
) -> list:
    """跨用户可见的全部 KB collection 检索（参考 WeKnora per-KB collection）。

    查全部 KB → 每个 KB 用各自绑定的 embedding 模型 embed query → 该 KB 的 collection
    内做向量+FTS 混合检索（ACL 预过滤由统一 policy 产出）→ 跨 KB 扩大候选池后统一重排（未配置/失败时关键词降级）。

    知识域为空（无权限）→ 不检索（默认隔离）。
    admin（tags=['*']）→ 搜全部 KB（source 不预过滤）。
    """
    if not cap.knowledge_scope_tags:
        return RetrievalOutcome()

    from bycall.config import get_settings
    from bycall.observability.metrics import RAG_RETRIEVAL_DURATION, RAG_RETRIEVED_CHUNKS
    from bycall.rag.retrieve import retrieve_multi_kb

    budget = resolve_budget(cap.context_budget_overrides)

    kbs = session.exec(_visible_kbs_statement(cap)).all()
    if not kbs:
        return RetrievalOutcome()

    kb_specs = [(kb.id, kb.collection_name, kb.dimension, kb.embedding_model_id) for kb in kbs]

    start = time.perf_counter()
    results = retrieve_multi_kb(
        session=session,
        config=get_settings().vector_store,
        kb_specs=kb_specs,
        question=question,
        knowledge_scope_tags=cap.knowledge_scope_tags,
        topk=budget.rag_topk,
        candidate_topk=budget.rag_candidate_topk,
        candidate_max_total=budget.rag_candidate_max_total,
        min_rerank_score=budget.rag_min_rerank_score,
        level=cap.level,
        is_admin=cap.is_admin,
    )
    elapsed = time.perf_counter() - start
    RAG_RETRIEVAL_DURATION.observe(elapsed)
    RAG_RETRIEVED_CHUNKS.observe(len(results))
    return results


def _build_citations(retrieved: list) -> list[dict]:
    """从检索结果构造引用元数据（M6 来源卡片用）。

    调用方必须传入预算化后实际注入的 evidence；snippet 与最终截断文本一致。
    稳定 chunk/doc/KB ID 和本轮 E 编号支持追溯，来源列表不代表逐句事实核验。
    最终卡片由 ``im_handler._build_msg_item`` 裁剪，缺失字段按空值兼容。
    """
    out: list[dict] = []
    for r in retrieved:
        fields = getattr(r, "fields", None) or {}
        out.append(
            {
                "title": str(fields.get("title") or ""),
                "source": str(fields.get("source") or ""),
                "url": str(fields.get("url") or ""),
                "snippet": str(getattr(r, "text", "") or ""),
                "reference": str(fields.get("reference") or ""),
                "chunk_id": str(fields.get("evidence_id") or getattr(r, "id", "")),
                "matched_chunk_id": str(getattr(r, "id", "")),
                "doc_id": str(fields.get("doc_id") or ""),
                "kb_id": fields.get("kb_id"),
            }
        )
    return out


@dataclass
class _PreparedTurn:
    """步骤 1-4 的治理前置管线产物（同步/流式两路共用，防行为漂移——缝隙 #6）。"""

    cap: CapabilitySet
    retrieved: list
    langchain_tools: list
    messages: list
    context_trace: str


def _prepare_turn(
    *,
    session: Session,
    user: User,
    question: str,
    channel: str,
    conversation_id: int | None,
    im_user_id: str,
    agent_id: int | None = None,
    llm: BaseChatModel | None = None,
) -> tuple[str, AgentResult | _PreparedTurn]:
    """步骤 1-4：能力集 → RAG 预过滤 → 工具过滤 → 运行时上下文 + 消息 + 能力/审批令牌。

    返回 ``(decision, payload)``：
    - ``("denied" | "denied_no_access", AgentResult)``：短路拒绝（已审计，调用方 return/流式回）。
    - ``("ready", _PreparedTurn)``：准备就绪，调用方执行工具回路。
    """
    # 1. 策略引擎：计算能力集（禁用用户/未定义角色/实例不可见在此被拒）。
    # 若未显式传 agent_id，但会话已归属某实例，则按会话实例计算（实例级隔离）。
    resolved_agent_id = agent_id
    if resolved_agent_id is None and conversation_id is not None:
        _conv = session.get(Conversation, conversation_id)
        if _conv is not None and _conv.agent_id is not None:
            resolved_agent_id = _conv.agent_id
    agent_obj = None
    if resolved_agent_id is not None:
        from bycall.governance.models import Agent

        agent_obj = session.get(Agent, resolved_agent_id)
        if agent_obj is None:
            _audit(
                session,
                user,
                channel,
                question,
                "denied",
                f"数字员工不存在: {resolved_agent_id}",
                "",
            )
            return "denied", _result(
                answer=f"数字员工不存在: {resolved_agent_id}",
                decision="denied",
                scope=[],
                retrieved=0,
                user=user,
            )
    try:
        with _tracer.start_as_current_span("capability") as _span:
            cap: CapabilitySet = compute_capability_set(session, user, agent_obj)
            _span.set_attribute("bycall.role", cap.role)
            _span.set_attribute("bycall.agent", cap.agent_name or "default")
            _span.set_attribute("bycall.allowed_tools", len(cap.allowed_tools))
    except PermissionError as e:
        _audit(session, user, channel, question, "denied", str(e), "")
        return "denied", _result(
            answer=f"权限不足：{e}",
            decision="denied",
            scope=[],
            retrieved=0,
            user=user,
        )

    # 2. RAG 检索（ACL 预过滤）。
    with _tracer.start_as_current_span("rag.retrieve") as _span:
        search_question = question
        history = []
        has_kbs = bool(session.exec(_visible_kbs_statement(cap).limit(1)).first())
        if llm is not None and conversation_id is not None and has_kbs and cap.knowledge_scope_tags:
            history = load_message_history(
                session, conversation_id, budget=resolve_budget(cap.context_budget_overrides)
            )
            if history:
                search_question = rewrite_query(llm, question, history_messages=history)
        retrieved = _retrieve_across_kbs(session, search_question, cap)
        if isinstance(retrieved, RetrievalOutcome):
            retrieved.query = search_question
            retrieved.rewrite_status = (
                "rewritten"
                if search_question != question
                else "original_or_fallback"
                if history
                else "not_requested"
            )
        _span.set_attribute("bycall.retrieved", len(retrieved))
        _span.set_attribute("bycall.has_scope", bool(cap.knowledge_scope_tags))

    # 无任何可见知识域 → 不进 LLM（不让 LLM 看到问题）。
    has_access = ("*" in cap.knowledge_scope_tags) or bool(cap.knowledge_scope_tags)
    if not has_access:
        _audit(session, user, channel, question, "denied_no_access", "无可见知识域", "")
        return "denied_no_access", _result(
            answer="你没有访问相关知识的权限，请联系管理员。",
            decision="denied_no_access",
            scope=cap.knowledge_scope_tags,
            retrieved=0,
            user=user,
        )

    # 3-4. 工具过滤 + 运行时上下文 + 消息构造。
    _allowed_tool_specs, langchain_tools, messages, context_trace = _prepare_agent_messages(
        session=session,
        user=user,
        cap=cap,
        question=question,
        channel=channel,
        conversation_id=conversation_id,
        retrieved=retrieved,
    )

    return "ready", _PreparedTurn(
        cap=cap,
        retrieved=retrieved,
        langchain_tools=langchain_tools,
        messages=messages,
        context_trace=context_trace,
    )


def _set_turn_context(cap, user, conversation_id, channel, im_user_id):
    """执行工具的线程/Task 自己安装令牌，不能跨 context 返回或 reset token。"""
    from bycall.mcp.context import set_current_capability
    from bycall.tools.approval import ApprovalContext, set_approval_context

    return set_current_capability(cap), set_approval_context(
        ApprovalContext(
            user_id=user.id,
            user_name=user.name,
            conversation_id=conversation_id,
            channel=channel,
            im_user_id=im_user_id,
        )
    )


def _prepare_stream_turn(bind, user_id: int, **kwargs):
    """工作线程独占自己的 Session；只返回消息、能力值与工具，不返回 ORM 实体。"""
    with Session(bind) as session:
        user = session.get(User, user_id)
        if user is None:
            raise PermissionError("用户已不存在")
        return _prepare_turn(session=session, user=user, **kwargs)


def _retrieval_notice(retrieved: list) -> str:
    """由运行时披露检索故障，不依赖模型自觉承认资料缺失。"""
    if not isinstance(retrieved, RetrievalOutcome):
        return ""
    if retrieved.status == "error":
        return "\n\n知识检索暂不可用，本次回答没有知识库证据。"
    if retrieved.status == "degraded":
        if not retrieved:
            return "\n\n知识检索已降级，本次未取得可用的知识库证据。"
        if any(i.get("stage") == "search" for i in retrieved.issues):
            return "\n\n部分知识库检索失败，参考资料可能不完整。"
        return "\n\n本次知识检索使用降级结果，参考资料可能不完整。"
    return ""


def _finalize_turn(
    *,
    session: Session,
    user: User,
    channel: str,
    question: str,
    cap: CapabilitySet,
    retrieved: list,
    graph_result: GraphResult,
    answer: str,
    context_trace: str,
    conversation_id: int | None,
) -> AgentResult:
    """步骤 6-7：会话持久化 + 审计落库 + 构造结果（两路共用，防行为漂移——缝隙 #6）。"""
    answer += _retrieval_notice(retrieved)
    # 6. 会话持久化：存 user + assistant 消息 + 压缩检查。
    if conversation_id is not None:
        save_message(session, conversation_id, "user", question)
        save_message(session, conversation_id, "assistant", answer)

    # 7. 审计落库（含工具调用记录）。
    tool_detail = ""
    if graph_result.tool_calls_made:
        tool_names = [tc["name"] for tc in graph_result.tool_calls_made]
        tool_detail = f"tools={tool_names}, "
    skill_detail = f"skills={cap.skills}, " if cap.skills else ""
    _audit(
        session,
        user,
        channel,
        question,
        "answered",
        f"{tool_detail}{skill_detail}retrieved={len(retrieved)} chunks, scope={cap.knowledge_scope_tags}",
        answer[:500],
        tool_results=json.dumps(graph_result.tool_calls_made, ensure_ascii=False),
        context_trace=context_trace,
    )
    # _audit 已提交历史与审计，后台读取不会早于事务提交。
    if conversation_id is not None and channel == "api":
        schedule_compaction(conversation_id)

    return _result(
        answer=answer,
        decision="answered",
        scope=cap.knowledge_scope_tags,
        retrieved=len(retrieved),
        user=user,
        tool_calls=graph_result.tool_calls_made,
        citations=json.loads(context_trace or "{}").get("rag_evidence", []),
        context_trace=context_trace,
    )


async def run_agent_stream(
    *,
    session: Session,
    user: User,
    question: str,
    llm: BaseChatModel,
    channel: str = "api",
    conversation_id: int | None = None,
    im_user_id: str = "",
    agent_id: int | None = None,
):
    """流式执行治理链路（观测包装）。

    逐 token 透传 ``_run_agent_stream_impl`` 的输出，并在结束帧统一记 T2.5 轮次指标。
    """
    start = time.perf_counter()
    final_result: AgentResult | None = None
    async for chunk_text, is_final, result in _run_agent_stream_impl(
        session=session,
        user=user,
        question=question,
        llm=llm,
        channel=channel,
        conversation_id=conversation_id,
        im_user_id=im_user_id,
        agent_id=agent_id,
    ):
        if is_final and result is not None:
            final_result = result
        yield chunk_text, is_final, result
    _observe_turn(final_result.decision if final_result is not None else "error", start)


async def _run_agent_stream_impl(
    *,
    session: Session,
    user: User,
    question: str,
    llm: BaseChatModel,
    channel: str = "api",
    conversation_id: int | None = None,
    im_user_id: str = "",
    agent_id: int | None = None,
):
    """流式执行治理链路——逐 token yield 答案。

    与 ``run_agent`` 同构，但用 ``run_tool_loop_stream`` 替代 ``run_tool_loop``，
    使 LLM 生成的每个 token 实时推送给调用方（IM / REST SSE）。

    Yields:
        ``(chunk_text: str, is_final: bool, result: AgentResult | None)``
        - 正常 token: ``("你好", False, None)``
        - 流结束: ``("", True, AgentResult(...))``
        - 错误: ``("错误信息", True, AgentResult(decision="error"))``

    stream_id 由调用方管理（IM 渠道用 sender.send_stream 的 stream_id），此处不生成。
    """

    # 仅传引擎和标量给工作线程；先释放入口事务，模型等待期间不占用该连接。
    bind = session.get_bind()
    user = User.model_validate(user)
    session.commit()
    if not _prepare_slots.acquire(blocking=False):
        result = _result(
            answer="服务繁忙，请稍后再试。", decision="busy", scope=[], retrieved=0, user=user
        )
        yield result.answer, True, result
        return
    try:
        future = _prepare_executor.submit(
            contextvars.copy_context().run,
            _prepare_stream_turn,
            bind,
            user.id,
            llm=llm,
            question=question,
            channel=channel,
            conversation_id=conversation_id,
            im_user_id=im_user_id,
            agent_id=agent_id,
        )
    except BaseException:
        _prepare_slots.release()
        raise
    # 取消等待不会提前归还仍在执行的工作槽位，也不会让调用方复用线程里的 Session。
    future.add_done_callback(lambda _: _prepare_slots.release())
    decision, payload = await asyncio.wrap_future(future)
    if decision != "ready":
        result = payload  # denied / denied_no_access（已审计）
        yield result.answer, True, result
        return

    cap = payload.cap
    retrieved = payload.retrieved
    langchain_tools = payload.langchain_tools
    messages = payload.messages
    context_trace = payload.context_trace
    cap_token, approval_token = _set_turn_context(cap, user, conversation_id, channel, im_user_id)

    # 5. 流式执行工具回路。
    from bycall.mcp.context import reset_current_capability
    from bycall.tools.approval import reset_approval_context

    full_answer = ""
    graph_result: GraphResult | None = None
    try:
        try:
            _chat_start = time.perf_counter()
            stream = await run_tool_loop_stream(
                chat_model=llm,
                tools=langchain_tools,
                messages=messages,
            )
            async for chunk_text, is_final, gr in stream:
                if is_final:
                    graph_result = gr
                    break
                if chunk_text is TOOL_ROUND_FLUSH:
                    # 工具回合前导文本（如「当前 UTC 时间是：」）已流式外送，现丢弃——
                    # 不计入最终答案，并透传给 im_handler 同步清空企微 replace 累积文本。
                    full_answer = ""
                    yield chunk_text, False, None
                    continue
                if chunk_text:
                    full_answer += chunk_text
                    yield chunk_text, False, None
            LLM_CALLS.labels(kind="chat", status="ok").inc()
            LLM_CALL_DURATION.labels(kind="chat").observe(time.perf_counter() - _chat_start)
        except Exception as e:
            logger.error("LLM/graph 流式执行失败: %s", e)
            LLM_CALLS.labels(kind="chat", status="error").inc()
            LLM_CALL_DURATION.labels(kind="chat").observe(time.perf_counter() - _chat_start)
            _audit(session, user, channel, question, "error", str(e)[:200], "")
            answer = "抱歉，服务暂时不可用。"
            yield (
                answer,
                True,
                _result(
                    answer=answer,
                    decision="error",
                    scope=cap.knowledge_scope_tags,
                    retrieved=len(retrieved),
                    user=user,
                ),
            )
            return
    finally:
        reset_current_capability(cap_token)
        reset_approval_context(approval_token)

    if graph_result is None:
        graph_result = GraphResult(answer=full_answer)
    answer = full_answer or graph_result.answer

    # 6-7. 会话持久化 + 审计 + 构造结果（两路共用）。
    result = _finalize_turn(
        session=session,
        user=user,
        channel=channel,
        question=question,
        cap=cap,
        retrieved=retrieved,
        graph_result=graph_result,
        answer=answer,
        context_trace=context_trace,
        conversation_id=conversation_id,
    )
    if graph_result.tool_calls_made:
        schedule_memory_extraction(llm=llm, user_id=user.id, question=question, answer=answer)
    notice = _retrieval_notice(retrieved)
    if notice:
        yield notice, False, None
    yield "", True, result


# ============ 任务触发（S1a T5：对话入口识别多步任务目标）============


_TASK_TRIGGER_PREFIXES = ("启动任务：", "启动任务:", "任务：", "任务:", "task:", "Task:")


def _match_task_trigger(question: str) -> str | None:
    """识别显式任务触发指令，返回提取的 goal（无匹配返回 None）。

    S1a：仅显式前缀触发（「启动任务：查故障」），降低误判。LLM 自动判定多步目标
    留后置（spec §3.5）。
    """
    for prefix in _TASK_TRIGGER_PREFIXES:
        if question.startswith(prefix):
            goal = question[len(prefix) :].strip()
            return goal or None
    return None


def _run_as_task(
    *,
    session: Session,
    user: User,
    question: str,
    goal: str,
    llm: BaseChatModel,
    channel: str,
    conversation_id: int | None,
) -> AgentResult:
    """对话触发的任务执行路径（S1a + S1b 多步拆解）。

    显式「启动任务：{goal}」触发：创建 Task → LLM 拆多步计划（建议性，代码层校验，
    失败降级单步）→ run_task 编排执行 → 取执行结果作 AgentResult.answer。
    任务模式不跑对话层 RAG（知识靠工具）。
    """
    from bycall.agent.task_engine import plan_task_goal, run_task
    from bycall.governance.models import Task

    task = Task(
        user_id=user.id,  # type: ignore[arg-type]
        conversation_id=conversation_id,
        channel=channel,
        trigger="conversation",
        goal=goal,
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    # S1b：LLM 把目标拆成多步计划；解析失败自动降级单步（goal 作唯一 tool_loop 步）。
    plan = plan_task_goal(llm, goal)
    run_task(session=session, task=task, chat_model=llm, plan=plan)

    answer = _task_answer(session, task)
    _audit(
        session,
        user,
        channel,
        question,
        "task",
        f"task={task.id},status={task.status},steps={len(plan)}",
        answer[:500],
    )
    # 任务执行路径同样触发 agent 记忆提取（spec：执行任务后提炼高置信新事实）。
    maybe_extract_and_save_memory(llm=llm, user_id=user.id, question=question, answer=answer)
    return _result(answer=answer, decision="task", scope=[], retrieved=0, user=user)


def _task_answer(session: Session, task) -> str:
    """从 task 执行结果提取回复（最后 Run 的 answer；无则按状态给消息）。"""
    from bycall.governance.models import Run, Step

    steps = list(session.exec(Step.select_by_task(task.id)))
    for step in reversed(steps):
        runs = list(session.exec(Run.select_by_step(step.id)))
        if runs:
            try:
                out = json.loads(runs[-1].output_json)
                if isinstance(out, dict) and out.get("answer"):
                    return str(out["answer"])
            except (json.JSONDecodeError, TypeError):
                pass
    return {
        "completed": "任务已完成。",
        "paused": "任务已暂停（等待审批或澄清）。",
        "failed": "任务执行失败。",
    }.get(task.status, f"任务状态：{task.status}")


__all__ = ["AgentResult", "run_agent", "run_agent_stream"]
