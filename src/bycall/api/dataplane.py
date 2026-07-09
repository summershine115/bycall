"""数据面 /chat 端点 —— 治理脊柱的端到端证明。

这条端点是 Bycall 核心论点的可执行验证：
**LLM 拿不到它无权拿的东西——不是"拿到后被拦"，是"请求时那个东西根本不在它的世界里"**。

链路（每一步都在 LLM 之前完成判定）：
  认证(会话) → 当前用户 → 能力集(策略引擎)
    → RAG 检索(ACL 预过滤：只召回 knowledge_scope_tags 内的 chunk)
    → LLM 调用(system=控制面下发的提示词, 用户输入隔离封装)
    → 审计(放行/拒绝决策落库)

dev 下：admin 可 act-as 普通用户，方便在无真实 IM 时验证权限链路。
真实 IM 接入（M2 LangBot）后，认证从"会话"换成"IM 可信令牌 + 身份映射"，链路其余不变。
"""

# FastAPI 的 Depends(...) 作为参数默认值是框架惯用法，与 ruff B008 冲突，整文件豁免。
# ruff: noqa: B008

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from bycall.config import get_settings
from bycall.db import get_session
from bycall.governance.auth import load_session_cookie
from bycall.governance.capability import compute_capability_set
from bycall.governance.models import AuditLog, User
from bycall.llm import LLMClient, Message
from bycall.llm.factory import create_llm_client
from bycall.vector_store.models import FilterCondition, SearchRequest

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
    retrieved_chunks: int  # 命中的 chunk 数（全是授权范围内的）
    decision: str  # answered / denied_no_access / error


def _current_user(request: Request, session: Session) -> User:
    """从会话 cookie 解析当前用户。

    dev act-as：若会话里有 act_as_user_id，扮演该普通用户（验证权限链路用）。
    生产环境（env=prod）禁止 act-as——auth.set_act_as 已守，这里二次校验。
    """
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


def _build_knowledge_filter(cap) -> list[FilterCondition]:
    """能力集 → RAG 预过滤条件。这是"权限活在检索层"的落点。

    knowledge_scope_tags=['*']（admin）→ 不过滤（全可见）。
    普通用户 → source IN (tags)，召回阶段就排除无权 chunk。
    空标签 → 一个永不命中的条件，效果等同"无可见知识"（默认隔离，不是默认公开）。
    """
    if "*" in cap.knowledge_scope_tags:
        return []
    if not cap.knowledge_scope_tags:
        # 无任何知识域权限：用一个不存在的 source 确保召回为空（隔离）。
        return [FilterCondition(field="source", op="match", value="__no_access__")]
    return [FilterCondition(field="source", op="in", values=cap.knowledge_scope_tags)]


# ---- 依赖：演示用向量库（M1 可选，无 zvec 数据时返回 None 跳过检索）----


def _get_demo_vector_store():
    """演示依赖：返回一个打开的 zvec 向量库（无则 None）。

    真实环境知识库由 admin 导入（M1 简化为直接写 zvec）。
    无向量库时 chat 仍可跑（退化为纯 LLM + 权限判定），便于最小化验证治理脊柱。
    """
    from bycall.vector_store.factory import create_vector_store

    try:
        store = create_vector_store(get_settings().vector_store)
        store.open("knowledge")  # 不存在则按默认 RAG schema 建
        return store
    except Exception:
        return None


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    request: Request,
    session: Session = Depends(get_session),
    llm: LLMClient = Depends(lambda: create_llm_client(get_settings().llm)),
    vector_store=Depends(_get_demo_vector_store),
) -> ChatResponse:
    """数据面对话：治理脊柱 + ACL 预过滤 RAG + 控制面提示词 + 审计。"""
    user = _current_user(request, session)

    # 1. 策略引擎：计算能力集（禁用用户/未定义角色在此被拒）。
    try:
        cap = compute_capability_set(session, user)
    except PermissionError as e:
        _audit(session, user, body.question, "denied", str(e), "")
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    # 2. RAG 检索（ACL 预过滤）：只召回 knowledge_scope 内的 chunk。
    filters = _build_knowledge_filter(cap)
    retrieved = []
    if vector_store is not None and filters != [
        FilterCondition(field="source", op="match", value="__no_access__")
    ]:
        try:
            retrieved = vector_store.search(
                SearchRequest(
                    full_text=body.question,
                    filters=filters,
                    topk=5,
                    output_fields=["text", "source"],
                )
            )
        except Exception as e:
            logger.warning("RAG 检索失败（降级为无上下文）: %s", e)

    # 无任何可见知识域 → 不进 LLM，直接拒绝（不让 LLM 看到问题、不留业务痕迹外的信息）。
    # 注意：有 scope 但召回为空（知识库里没有相关文档）不算"无权限"，仍走 LLM（无上下文）。
    has_access = ("*" in cap.knowledge_scope_tags) or bool(cap.knowledge_scope_tags)
    if not has_access:
        _audit(session, user, body.question, "denied_no_access", "无可见知识域", "")
        return ChatResponse(
            answer="你没有访问相关知识的权限，请联系管理员。",
            user=user.name,
            role=user.role,
            knowledge_scope=cap.knowledge_scope_tags,
            retrieved_chunks=0,
            decision="denied_no_access",
        )

    # 3. 拼 prompt：system 来自控制面（激活提示词），用户输入作为隔离的 user message。
    context = "\n\n".join(f"[{r.fields.get('source', '?')}] {r.text}" for r in retrieved)
    messages = [
        Message(role="system", content=cap.system_prompt),
        Message(
            role="system",
            content=f"以下是知识库内容（均已确认你有权访问），请基于它回答：\n{context}",
        ),
        Message(role="user", content=body.question),
    ]

    # 4. LLM 调用。失败降级但不暴露内部错误。
    try:
        answer = llm.chat(messages)
    except Exception as e:
        logger.error("LLM 调用失败: %s", e)
        _audit(session, user, body.question, "error", str(e)[:200], "")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "LLM 服务不可用") from e

    # 5. 审计落库。
    _audit(
        session,
        user,
        body.question,
        "answered",
        f"retrieved={len(retrieved)} chunks, scope={cap.knowledge_scope_tags}",
        answer[:500],
    )

    return ChatResponse(
        answer=answer,
        user=user.name,
        role=user.role,
        knowledge_scope=cap.knowledge_scope_tags,
        retrieved_chunks=len(retrieved),
        decision="answered",
    )


def _audit(
    session: Session, user: User, question: str, decision: str, detail: str, answer: str
) -> None:
    """写一条审计日志（append-only）。"""
    session.add(
        AuditLog(
            user_id=user.id,
            user_name=user.name,
            channel="api",
            action="chat",
            question=question[:500],
            decision=decision,
            detail=detail,
            answer=answer[:500],
        )
    )
    session.commit()
