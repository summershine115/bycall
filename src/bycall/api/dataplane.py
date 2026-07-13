"""数据面 /chat 端点 —— 治理链路的 REST 入口（Admin 控制台用）。

核心逻辑在 ``agent/runner.py`` 的 ``run_agent``——控制面(本端点)和数据面(IM 渠道)共用。
这里只做：会话认证 → 取当前用户 → 调 run_agent → 包装成 HTTP 响应。

dev 下：admin 可 act-as 普通用户，方便在无真实 IM 时验证权限链路。
真实 IM 接入（gateway/wecom.py）后，IM 消息走 ``agent/im_handler.py``，链路其余不变。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from bycall.agent.runner import run_agent
from bycall.config import get_settings
from bycall.db import get_session
from bycall.governance.auth import load_session_cookie
from bycall.governance.models import User

# FastAPI 的 Depends(...) 作为参数默认值是框架惯用法，与 ruff B008 冲突，整文件豁免。
# ruff: noqa: B008

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dataplane"])


class ChatRequest(BaseModel):
    """数据面对话请求。question 来自用户输入。"""

    question: str


class ChatResponse(BaseModel):
    """对话响应 + 透明化治理决策（便于演示/审计，非越权信息）。"""

    answer: str
    user: str
    role: str
    knowledge_scope: list[str]
    retrieved_chunks: int
    decision: str


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
    from bycall.agent.session_store import get_or_create_conversation
    from bycall.llm.resolver import get_chat_model

    user = _current_user(request, session)
    # 从 DB 激活的 chat Model 实体构造 ChatModel；无激活模型则报错提示
    resolved = get_chat_model(session)
    if resolved is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "未激活对话模型，请管理员在「模型管理」激活一个 chat 模型",
        )
    _, chat_model = resolved

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
    return ChatResponse(
        answer=result.answer,
        user=result.user_name,
        role=result.role,
        knowledge_scope=result.knowledge_scope,
        retrieved_chunks=result.retrieved_chunks,
        decision=result.decision,
    )
