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

import atexit
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlmodel import Session

from bycall.agent.graph import TOOL_ROUND_FLUSH, GraphResult, run_tool_loop, run_tool_loop_stream
from bycall.agent.runtime_context import build_runtime_context
from bycall.agent.session_store import (
    build_context_trace,
    build_conversation_messages,
    compress_conversation,
    load_message_history,
    load_user_memory,
    save_message,
)
from bycall.db import session_scope
from bycall.governance.capability import CapabilitySet, compute_capability_set
from bycall.governance.models import AuditLog, Conversation, KnowledgeBase, User
from bycall.llm.base import Message as LLMMessage
from bycall.observability import get_tracer, traced
from bycall.observability.metrics import LLM_CALL_DURATION, LLM_CALLS
from bycall.rag.retrieve import build_rag_messages
from bycall.tools.gateway import filter_tools, to_langchain_tools
from bycall.tools.registry import get_tool

logger = logging.getLogger(__name__)


# ============ 后台压缩（context engineering：压缩不阻塞请求） ============

# 测试置 True → 同步压缩（确定性好断言）；生产保持 False（后台线程）。
_COMPACTION_SYNC = False
_compactor_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="compaction")
_compactor_locks: dict[int, threading.Lock] = {}
_compactor_locks_guard = threading.Lock()
atexit.register(_compactor_executor.shutdown, wait=False)


def _compactor_lock_for(conversation_id: int) -> threading.Lock:
    with _compactor_locks_guard:
        lock = _compactor_locks.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _compactor_locks[conversation_id] = lock
        return lock


def _run_compaction(conversation_id: int) -> None:
    """后台压缩一个会话（自包含：开自己的 session + 取 chat model + 压缩 + 提交）。"""
    from bycall.llm import resolver

    lock = _compactor_lock_for(conversation_id)
    if not lock.acquire(blocking=False):
        return  # 该会话已在压缩，跳过；下一轮 should_compress 会再追
    try:
        with session_scope() as session:
            try:
                _model, chat_model = resolver.get_chat_model(session)
            except Exception as e:  # 取不到模型（如未配置）→ 跳过，不阻断
                logger.warning("压缩取模型失败 conv=%s: %s", conversation_id, e)
                return
            compress_conversation(session, conversation_id, chat_model)
    except Exception:
        logger.exception("后台压缩失败 conv=%s", conversation_id)
    finally:
        lock.release()


def schedule_compaction(conversation_id: int) -> None:
    """异步压缩会话历史（不阻塞用户回复）。幂等可重入；指针模型保证不丢、不重复。"""
    if _COMPACTION_SYNC:
        _run_compaction(conversation_id)
        return
    _compactor_executor.submit(_run_compaction, conversation_id)


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
    # 1. 策略引擎：计算能力集（禁用用户/未定义角色在此被拒）。
    try:
        with _tracer.start_as_current_span("capability") as _span:
            cap: CapabilitySet = compute_capability_set(session, user)
            _span.set_attribute("bycall.role", cap.role)
            _span.set_attribute("bycall.allowed_tools", len(cap.allowed_tools))
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
    with _tracer.start_as_current_span("rag.retrieve") as _span:
        retrieved = _retrieve_across_kbs(
            session, question, cap.knowledge_scope_tags, cap.visible_kb_ids, cap.is_admin
        )
        _span.set_attribute("bycall.retrieved", len(retrieved))
        _span.set_attribute("bycall.has_scope", bool(cap.knowledge_scope_tags))

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

    # 3-4. 工具过滤 + 运行时上下文 + 消息构造（同步/流式共用，防行为漂移）。
    _allowed_tool_specs, langchain_tools, messages, context_trace = _prepare_agent_messages(
        session=session,
        user=user,
        cap=cap,
        question=question,
        channel=channel,
        conversation_id=conversation_id,
        retrieved=retrieved,
    )

    # 5. 执行工具回路（LangGraph）：model ↔ tool 循环，直到无 tool_calls。
    #    能力令牌（M4 Slice B）：通过 contextvars 注入当前 CapabilitySet，MCPClient.call_tool
    #    读取并编码为 meta 传给 MCP server 自行鉴权。
    #    审批上下文（M4 Slice C）：携带 user/conversation/channel，供审批门创建
    #    ApprovalRequest（破坏性工具调用人审拦截）。
    from bycall.mcp.context import reset_current_capability, set_current_capability
    from bycall.tools.approval import (
        ApprovalContext,
        reset_approval_context,
        set_approval_context,
    )

    cap_token = set_current_capability(cap)
    approval_token = set_approval_context(
        ApprovalContext(
            user_id=user.id,  # type: ignore[arg-type]
            user_name=user.name,
            conversation_id=conversation_id,
            channel=channel,
            im_user_id=im_user_id,
        )
    )
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

    # 6. 会话持久化：存 user + assistant 消息 + 压缩检查。
    if conversation_id is not None:
        save_message(session, conversation_id, "user", question)
        save_message(session, conversation_id, "assistant", answer)

        # 压缩改后台异步：指针判定与折叠在 compress_conversation 内，不阻塞回复。
        schedule_compaction(conversation_id)

    # 7. 审计落库（含工具调用记录）。
    # detail 保留工具名/skill 名摘要（快速扫），tool_results 存结构化明细 [{name,args,result}]
    # （result 为脱敏后值，支柱 3「调用后审计落库」+ 支柱 4 第二条泄漏面）。
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

    return _result(
        answer=answer,
        decision="answered",
        scope=cap.knowledge_scope_tags,
        retrieved=len(retrieved),
        user=user,
        tool_calls=graph_result.tool_calls_made,
        citations=_build_citations(retrieved),
    )


def _result(
    *,
    answer: str,
    decision: str,
    scope: list[str],
    retrieved: int,
    user: User,
    tool_calls: list[dict] | None = None,
    citations: list[dict] | None = None,
) -> AgentResult:
    return AgentResult(
        answer=answer,
        decision=decision,
        knowledge_scope=list(scope),
        retrieved_chunks=retrieved,
        user_name=user.name,
        role=user.role,
        tool_calls=tool_calls or [],
        citations=citations or [],
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
    allowed_tool_specs = filter_tools(cap.allowed_tools)
    if cap.on_demand_skills:
        loader = get_tool("load_skill")
        if loader and loader not in allowed_tool_specs:
            allowed_tool_specs.append(loader)
    langchain_tools = to_langchain_tools(allowed_tool_specs, cap)

    history = []
    summary = ""
    user_memories = (
        load_user_memory(session, user.id, question=question) if user.id is not None else []
    )
    if conversation_id is not None:
        conv = session.get(Conversation, conversation_id)
        summary = conv.summary if conv is not None else ""
        history = load_message_history(session, conversation_id)

    runtime_context = build_runtime_context(
        user=user,
        capability=cap,
        channel=channel,
        conversation_id=conversation_id,
        tools=allowed_tool_specs,
        has_history=bool(history),
        has_summary=bool(summary),
        memory_count=len(user_memories),
    )
    if conversation_id is not None:
        rag_context = "\n\n".join(f"[{r.fields.get('source', '?')}] {r.text}" for r in retrieved)
        raw_messages = build_conversation_messages(
            system_prompt=cap.system_prompt,
            rag_context=rag_context,
            question=question,
            history=history,
            summary=summary,
            user_memories=user_memories,
            skill_markdown=cap.skill_markdown,
            knowledge_scope_text=cap.knowledge_scope_text,
            runtime_context=runtime_context,
        )
    else:
        raw_messages = build_rag_messages(
            cap.system_prompt,
            question,
            retrieved,
            skill_markdown=cap.skill_markdown,
            knowledge_scope_text=cap.knowledge_scope_text,
            user_memories=user_memories,
            runtime_context=runtime_context,
        )
    context_trace = build_context_trace(
        raw_messages=raw_messages,
        retrieved=retrieved,
        user_memories=user_memories,
        summary=summary,
        history=history,
    )
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


def _retrieve_across_kbs(
    session: Session,
    question: str,
    knowledge_scope_tags: list[str],
    visible_kb_ids: list[int],
    is_admin: bool,
) -> list:
    """跨用户可见的全部 KB collection 检索（参考 WeKnora per-KB collection）。

    查全部 KB → 每个 KB 用各自绑定的 embedding 模型 embed query → 该 KB 的 collection
    内做向量+FTS 混合检索（source ACL 预过滤）→ 跨 KB 用 RRF 融合取 topk。

    知识域为空（无权限）→ 不检索（默认隔离）。
    admin（tags=['*']）→ 搜全部 KB（source 不预过滤）。
    """
    if not knowledge_scope_tags:
        return []

    from bycall.config import get_settings
    from bycall.observability.metrics import RAG_RETRIEVAL_DURATION, RAG_RETRIEVED_CHUNKS
    from bycall.rag.retrieve import retrieve_multi_kb

    kbs = list(session.exec(KnowledgeBase.select_all()).all())
    if not is_admin:
        visible_ids = set(visible_kb_ids)
        kbs = [kb for kb in kbs if kb.id in visible_ids]
    if not kbs:
        return []

    kb_specs = [(kb.id, kb.collection_name, kb.dimension, kb.embedding_model_id) for kb in kbs]

    start = time.perf_counter()
    results = retrieve_multi_kb(
        session=session,
        config=get_settings().vector_store,
        kb_specs=kb_specs,
        question=question,
        knowledge_scope_tags=knowledge_scope_tags,
    )
    elapsed = time.perf_counter() - start
    RAG_RETRIEVAL_DURATION.observe(elapsed)
    RAG_RETRIEVED_CHUNKS.observe(len(results))
    return results


def _build_citations(retrieved: list) -> list[dict]:
    """从检索结果构造引用元数据（M6 来源卡片用）。

    每条暴露 title/source/url/snippet，最终卡片形状（去重/截断/跳过无标题）由
    ``im_handler._build_msg_item`` 裁剪——runner 只做机械映射，不丢任何来源。
    ``SearchResult.fields`` 实际回显字段由向量库 output_fields 决定（至少含 source），
    这里一律用 ``.get`` 容错缺失。
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
            }
        )
    return out


async def run_agent_stream(
    *,
    session: Session,
    user: User,
    question: str,
    llm: BaseChatModel,
    channel: str = "api",
    conversation_id: int | None = None,
    im_user_id: str = "",
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

    # 1. 策略引擎。
    try:
        cap: CapabilitySet = compute_capability_set(session, user)
    except PermissionError as e:
        _audit(session, user, channel, question, "denied", str(e), "")
        yield (
            _result(
                answer=f"权限不足：{e}",
                decision="denied",
                scope=[],
                retrieved=0,
                user=user,
            ).answer,
            True,
            _result(
                answer=f"权限不足：{e}",
                decision="denied",
                scope=[],
                retrieved=0,
                user=user,
            ),
        )
        return

    # 2. RAG 检索。
    retrieved = _retrieve_across_kbs(
        session, question, cap.knowledge_scope_tags, cap.visible_kb_ids, cap.is_admin
    )
    has_access = ("*" in cap.knowledge_scope_tags) or bool(cap.knowledge_scope_tags)
    if not has_access:
        _audit(session, user, channel, question, "denied_no_access", "无可见知识域", "")
        answer = "你没有访问相关知识的权限，请联系管理员。"
        yield (
            answer,
            True,
            _result(
                answer=answer,
                decision="denied_no_access",
                scope=cap.knowledge_scope_tags,
                retrieved=0,
                user=user,
            ),
        )
        return

    # 3-4. 工具过滤 + 运行时上下文 + 消息构造（与同步入口共享）。
    _allowed_tool_specs, langchain_tools, messages, context_trace = _prepare_agent_messages(
        session=session,
        user=user,
        cap=cap,
        question=question,
        channel=channel,
        conversation_id=conversation_id,
        retrieved=retrieved,
    )

    # 5. 流式执行工具回路。
    #    能力令牌：注入 CapabilitySet 供 MCPClient.call_tool 使用。
    #    审批上下文（M4 Slice C）：供审批门创建 ApprovalRequest。
    from bycall.mcp.context import reset_current_capability, set_current_capability
    from bycall.tools.approval import (
        ApprovalContext,
        reset_approval_context,
        set_approval_context,
    )

    cap_token = set_current_capability(cap)
    approval_token = set_approval_context(
        ApprovalContext(
            user_id=user.id,  # type: ignore[arg-type]
            user_name=user.name,
            conversation_id=conversation_id,
            channel=channel,
            im_user_id=im_user_id,
        )
    )
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

    # 6. 会话持久化。
    if conversation_id is not None:
        save_message(session, conversation_id, "user", question)
        save_message(session, conversation_id, "assistant", answer)
        # 压缩改后台异步：指针判定与折叠在 compress_conversation 内，不阻塞回复。
        schedule_compaction(conversation_id)

    # 7. 审计。
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

    result = _result(
        answer=answer,
        decision="answered",
        scope=cap.knowledge_scope_tags,
        retrieved=len(retrieved),
        user=user,
        tool_calls=graph_result.tool_calls_made,
        citations=_build_citations(retrieved),
    )
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
