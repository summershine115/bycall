"""数据面 /chat 端点 —— 治理链路的 REST 入口（Admin 控制台用）。

核心逻辑在 ``agent/runner.py`` 的 ``run_agent``——控制面(本端点)和数据面(IM 渠道)共用。
这里只做：会话认证 → 取当前用户 → 调 run_agent → 包装成 HTTP 响应。

api 渠道支持多会话：一个用户可同时持有多个「对话测试」会话，互不干扰。
- 不传 ``conversation_id`` → 新建会话（唯一 session_key，不影响 IM 渠道单会话语义）。
- 传 ``conversation_id`` → 继续该会话（校验归属 + 渠道）。
IM 渠道仍由 ``get_or_create_conversation`` 按 ``{channel}:{user_id}`` 单会话管理，不受影响。
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from bycall.agent.graph import TOOL_ROUND_FLUSH
from bycall.agent.runner import run_agent, run_agent_stream
from bycall.agent.session_store import load_message_history
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
    """数据面对话请求。

    ``conversation_id`` 为空 → 新建 api 会话；非空 → 继续该会话（校验归属）。
    """

    question: str
    conversation_id: int | None = None


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


class ConversationSummary(BaseModel):
    """会话列表项：当前用户的一个 api 会话摘要。"""

    id: int
    message_count: int
    last_message_at: str  # ISO 字符串，前端按需格式化
    preview: str  # 首条 user 消息预览（对话主题），无消息则占位


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


def _resolve_chat_model(session: Session, conversation: Conversation | None = None):
    """取对话 ChatModel：per-session 模型优先，否则全局激活；无则抛 503。"""
    from bycall.llm.resolver import get_chat_model_for_conversation

    resolved = get_chat_model_for_conversation(session, conversation)
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


def _new_api_conversation(session: Session, user: User) -> Conversation:
    """新建一个 api 渠道会话。

    api 渠道支持多会话：每次新建用唯一 session_key（``api:{uid}:{rand}``），
    不与 IM 渠道的 ``{channel}:{uid}`` 单会话查找冲突。
    """
    conv = Conversation(
        user_id=user.id,  # type: ignore[arg-type]
        channel="api",
        session_key=f"api:{user.id}:{uuid.uuid4().hex[:8]}",
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)
    logger.info("新建 api 会话: conv_id=%s user=%s", conv.id, user.name)
    return conv


def _get_owned_conversation(session: Session, user: User, conv_id: int) -> Conversation:
    """取当前用户的一个 api 会话（校验归属 + 渠道，防越权恢复他人/IM 会话）。"""
    conv = session.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id or conv.channel != "api":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问")
    return conv


def _resolve_conversation(session: Session, user: User, conv_id: int | None) -> Conversation:
    """按 conversation_id 决定新建或继续（/chat 与 /chat/stream 共用）。"""
    if conv_id is not None:
        return _get_owned_conversation(session, user, conv_id)
    return _new_api_conversation(session, user)


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

    conversation_id 为空 → 新建 api 会话；非空 → 继续（校验归属）。
    """
    if len(body.question) > MAX_QUESTION_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"问题过长（{len(body.question)} 字符，上限 {MAX_QUESTION_LEN}）",
        )

    user = _current_user(request, session)
    _check_rate_limit(user)
    conv = _resolve_conversation(session, user, body.conversation_id)
    chat_model = _resolve_chat_model(session, conv)

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
        _check_rate_limit(user)
        conv = _resolve_conversation(session, user, body.conversation_id)
        conv_id = conv.id
        chat_model = _resolve_chat_model(session, conv)
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


@router.get("/chat/conversations", response_model=list[ConversationSummary])
def chat_conversations(
    request: Request,
    session: Session = Depends(get_session),
) -> list[ConversationSummary]:
    """当前用户的全部 api 会话（按最后消息时间倒序）。

    每个会话带首条 user 消息预览作为标题，便于在侧栏区分。
    """
    user = _current_user(request, session)
    convs = session.exec(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.channel == "api")
        .order_by(Conversation.last_message_at.desc())
    ).all()
    result = []
    for conv in convs:
        first_user = session.exec(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.role == "user")
            .order_by(Message.id)
            .limit(1)
        ).first()
        preview = (first_user.content[:40] if first_user else "") or "（新对话）"
        result.append(
            ConversationSummary(
                id=conv.id,
                message_count=conv.message_count,
                last_message_at=conv.last_message_at.isoformat(),
                preview=preview,
            )
        )
    return result


@router.get("/chat/conversations/{conv_id}/messages", response_model=list[ChatMessageOut])
def chat_conversation_messages(
    conv_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> list[ChatMessageOut]:
    """某会话的消息历史（切换会话时加载，校验归属）。"""
    user = _current_user(request, session)
    conv = _get_owned_conversation(session, user, conv_id)
    rows = load_message_history(session, conv.id, limit=100)
    return [ChatMessageOut(role=m.role, content=m.content) for m in rows]


@router.delete("/chat/conversations/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
def chat_delete_conversation(
    conv_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> None:
    """删除当前用户的一个 api 会话（消息 + 会话记录，校验归属）。"""
    user = _current_user(request, session)
    conv = _get_owned_conversation(session, user, conv_id)
    msgs = session.exec(Message.select_by_conversation(conv.id)).all()
    for msg in msgs:
        session.delete(msg)
    session.delete(conv)
    session.commit()
