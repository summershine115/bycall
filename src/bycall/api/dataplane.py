"""数据面 /chat 端点 —— 治理链路的 REST 入口（Admin 控制台用）。

核心逻辑在 ``agent/runner.py`` 的 ``run_agent``——控制面(本端点)和数据面(IM 渠道)共用。
这里只做：会话认证 → 取当前用户 → 调 run_agent → 包装成 HTTP 响应。

除同步 ``/chat`` 外，提供流式 ``/chat/stream``（SSE 逐 token）、会话历史
``/chat/history``、会话重置 ``/chat/reset``——让控制台「对话测试」页能逐 token
观察治理决策，并把后端已产出的工具调用记录、RAG 引用来源、上下文快照完整呈现。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session

from bycall.agent.graph import TOOL_ROUND_FLUSH
from bycall.agent.runner import run_agent, run_agent_stream
from bycall.agent.session_store import get_or_create_conversation, load_message_history
from bycall.config import get_settings
from bycall.db import get_session, session_scope
from bycall.governance.auth import load_session_cookie
from bycall.governance.models import Conversation, Message, User

# FastAPI 的 Depends(...) 作为参数默认值是框架惯用法，与 ruff B008 冲突，整文件豁免。
# ruff: noqa: B008

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dataplane"])

# 问题长度上限：防止超长输入拖慢检索 + LLM 费用 DoS。
# 4000 字符 ≈ 2000-4000 token（中英混合），足够任何正常提问。
MAX_QUESTION_LEN = 4000


class ChatRequest(BaseModel):
    """数据面对话请求。question 来自用户输入。"""

    question: str


class ChatResponse(BaseModel):
    """对话响应 + 透明化治理决策（便于演示/审计，非越权信息）。

    ``tool_calls`` / ``citations`` / ``context_trace`` 是后端治理链路已产出、
    但此前未透传给前端的三类排障数据：LLM 本轮调了哪些工具、答案来自哪些文档、
    模型这一轮实际看到了什么（system/RAG/记忆/摘要快照）。
    """

    answer: str
    user: str
    role: str
    knowledge_scope: list[str]
    retrieved_chunks: int
    decision: str
    conversation_id: int | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    context_trace: str = ""


class ChatMessageOut(BaseModel):
    """历史消息（对话测试页加载会话历史用，只取展示所需字段）。"""

    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    """当前用户 api 会话的历史消息 + 会话 id。"""

    conversation_id: int | None = None
    messages: list[ChatMessageOut]


def _current_user(request: Request, session: Session) -> User:
    """从会话 cookie 解析当前用户（dev act-as 支持）。"""
    token = request.cookies.get("bycall_session")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    sess = load_session_cookie(token)
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话无效或已过期")

    # dev 扮演：admin 以普通用户身份测试数据面
    target_id = sess.act_as_user_id
    if target_id is not None:
        if get_settings().env != "dev":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "act-as 仅 dev 允许")
        user = session.get(User, target_id)
    else:
        user = session.get(User, sess.user_id)

    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "用户不存在或已禁用")
    return user


def _resolve_chat_model(session: Session):
    """取激活的 chat Model；无则抛 503。返回 LangChain ChatModel。"""
    from bycall.llm.resolver import get_chat_model

    resolved = get_chat_model(session)
    if resolved is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "未激活对话模型，请管理员在「模型管理」激活一个 chat 模型",
        )
    return resolved[1]


def _check_rate_limit(user: User) -> None:
    """按 user_id 维度限流（30 次/分钟，防费用 DoS）。"""
    from bycall.governance.ratelimit import RATE_LIMIT_CHAT, get_limiter

    if not get_limiter().allow(f"chat:{user.id}", **RATE_LIMIT_CHAT):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")


def _to_response(result, conv_id: int | None) -> ChatResponse:
    """AgentResult → ChatResponse（同步 / 流式 done 共用）。"""
    return ChatResponse(
        answer=result.answer,
        user=result.user_name,
        role=result.role,
        knowledge_scope=result.knowledge_scope,
        retrieved_chunks=result.retrieved_chunks,
        decision=result.decision,
        conversation_id=conv_id,
        tool_calls=result.tool_calls,
        citations=result.citations,
        context_trace=result.context_trace,
    )


def _sse(event: str, data: str) -> str:
    """格式化一个 SSE 事件帧（event + data + 空行结束）。"""
    return f"event: {event}\ndata: {data}\n\n"


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> ChatResponse:
    """数据面对话：委托 run_agent 执行完整治理链路。

    LLM/EmbedModel 从 DB 激活的 Model 实体解析（admin 控制台管理）。
    RAG 检索由 run_agent 内部查 KB 路由（不再从外部传 vector_store）。
    """
    if len(body.question) > MAX_QUESTION_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"问题过长（{len(body.question)} 字符，上限 {MAX_QUESTION_LEN}）",
        )

    user = _current_user(request, session)
    chat_model = _resolve_chat_model(session)
    _check_rate_limit(user)

    # 多轮会话：按 (user_id, channel) 查/建会话，传入 run_agent 启用历史。
    conv = get_or_create_conversation(session, user.id, "api")
    result = run_agent(
        session=session,
        user=user,
        question=body.question,
        llm=chat_model,
        channel="api",
        conversation_id=conv.id,
    )
    return _to_response(result, conv.id)


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
) -> StreamingResponse:
    """流式数据面对话：SSE 逐 token 推送答案，结束 ``done`` 事件带治理元数据。

    SSE 事件：
    - ``token`` ``{"text": "..."}``：追加到当前答案气泡。
    - ``flush`` ``{}``：工具回合前导文本需丢弃，清空当前累积（与 IM 侧同语义）。
    - ``done``：``ChatResponse`` JSON（含 tool_calls/citations/context_trace）。
    - ``error`` ``{"message": "..."}``：流式期间异常。

    认证/限流在进入流之前完成（错误以 HTTP 状态码返回，而非 SSE）。
    session 不能用 Depends——StreamingResponse 的生成器在依赖清理后才运行，
    故手动开 session_scope 保持流式期间存活（与 im_handler._run_stream 同构）。
    """
    if len(body.question) > MAX_QUESTION_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"问题过长（{len(body.question)} 字符，上限 {MAX_QUESTION_LEN}）",
        )

    ctx = session_scope()
    session = ctx.__enter__()
    try:
        user = _current_user(request, session)
        chat_model = _resolve_chat_model(session)
        _check_rate_limit(user)
        conv = get_or_create_conversation(session, user.id, "api")
        conv_id = conv.id
    except BaseException:
        ctx.__exit__(None, None, None)
        raise

    async def event_stream():
        try:
            async for chunk_text, is_final, result in run_agent_stream(
                session=session,
                user=user,
                question=body.question,
                llm=chat_model,
                channel="api",
                conversation_id=conv_id,
            ):
                if is_final and result is not None:
                    yield _sse("done", _to_response(result, conv_id).model_dump_json())
                elif chunk_text is TOOL_ROUND_FLUSH:
                    # 工具回合前导文本（如「正在查询…」）已流式外送，现丢弃——
                    # 前端清空当前累积，下一回合真实答案会覆盖。
                    yield _sse("flush", "{}")
                elif chunk_text:
                    yield _sse("token", json.dumps({"text": chunk_text}, ensure_ascii=False))
        except Exception as e:
            logger.error("流式对话失败: %s", e, exc_info=True)
            yield _sse("error", json.dumps({"message": str(e)[:200]}, ensure_ascii=False))
        finally:
            try:
                session.commit()
            except Exception:
                logger.warning("流式 session 提交失败，已忽略", exc_info=True)
            ctx.__exit__(None, None, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/chat/history", response_model=ChatHistoryResponse)
def chat_history(
    request: Request,
    session: Session = Depends(get_session),
) -> ChatHistoryResponse:
    """当前用户 api 会话的历史消息（对话测试页加载用）。

    只查不建——无会话时返回空列表（首次进入页面尚未发过消息）。
    """
    user = _current_user(request, session)
    session_key = f"api:{user.id}"
    conv = session.exec(Conversation.select_by_session_key(session_key)).first()
    if conv is None:
        return ChatHistoryResponse(conversation_id=None, messages=[])
    rows = load_message_history(session, conv.id, limit=100)
    return ChatHistoryResponse(
        conversation_id=conv.id,
        messages=[ChatMessageOut(role=m.role, content=m.content) for m in rows],
    )


@router.post("/chat/reset")
def chat_reset(
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """重置当前用户 api 会话：清空消息 + summary + 压缩指针，保留会话本身。

    与 admin ``/conversations/{id}/reset`` 同语义，但面向当前数据面用户自服务
    （对话测试页「新对话」按钮）。额外重置 ``summary_covered_count``——
    admin 侧遗漏了压缩指针归零，此处做对，避免重置后压缩指针错乱。
    """
    user = _current_user(request, session)
    session_key = f"api:{user.id}"
    conv = session.exec(Conversation.select_by_session_key(session_key)).first()
    if conv is None:
        return {"conversation_id": None, "reset": False}

    msgs = session.exec(Message.select_by_conversation(conv.id)).all()
    for msg in msgs:
        session.delete(msg)
    conv.summary = ""
    conv.message_count = 0
    conv.summary_covered_count = 0
    session.add(conv)
    session.commit()
    return {"conversation_id": conv.id, "reset": True}
