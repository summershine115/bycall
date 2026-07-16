"""IM 消息处理器 —— 把渠道入站消息接入治理链路。

收到 IM 消息后：
  身份映射(IM user_id → User) → run_agent(治理链路) → 流式回复

未绑定用户（未邀请）→ 友好拒绝；无权知识 → 拒绝（审计落库）；正常 → 流式回复答案。
"""

from __future__ import annotations

import asyncio
import logging

from bycall.agent.runner import run_agent
from bycall.db import session_scope
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.gateway.identity import resolve_user_by_im

logger = logging.getLogger(__name__)

MAX_IM_QUESTION_LEN = 4000


async def handle_im_message(msg: InboundMessage, sender: ReplySender) -> None:
    """IM 消息 → 治理链路 → 回复。

    每条消息独立 session（IM 是长连接事件流，不复用请求级 session）。
    LLM/vector_store 每次新建（M1.5 简化；M5 可换池化）。

    ⚠️ 群聊消息在权限模型完成前一律拒绝：当前 session_key 仅 ``{channel}:{user_id}``，
    不含 chat_id——群聊会让有权限用户的机密答案发到整个群、私聊与群聊串历史。
    拒绝发生在身份映射之前（不泄露是否绑定、是否有权）。
    """
    text = msg.text.strip()
    if not text:
        return

    if len(text) > MAX_IM_QUESTION_LEN:
        await sender.send_text(f"问题过长，请控制在 {MAX_IM_QUESTION_LEN} 字符以内。")
        return

    # 0. 群聊守卫：chat_id 非空 = 群聊消息，在群聊权限模型完成前直接拒绝。
    if msg.chat_id:
        await sender.send_text("群聊暂未开放，请在私聊中使用数字员工。")
        logger.info("IM 群聊消息被拒: channel=%s chat_id=%s", msg.channel, msg.chat_id)
        return

    # 1. 身份映射：IM user_id → Bycall User（邀请制，未绑定则拒绝）。
    #    在 session 内提取标量 user_id（避免 session 关闭后访问 detached 对象）。
    with session_scope() as session:
        user = resolve_user_by_im(session, msg.channel, msg.im_user_id)
        user_id = user.id if user is not None else None

    if user_id is None:
        await sender.send_text("您还未被授权使用数字员工，请联系管理员邀请您加入。")
        logger.info(
            "IM 消息被拒（未绑定用户）: channel=%s im_user_id=%s", msg.channel, msg.im_user_id
        )
        return

    # REST 与 IM 共用 user_id 额度，避免换渠道绕过费用限流。
    from bycall.governance.ratelimit import RATE_LIMIT_CHAT, get_limiter

    allowed = await asyncio.to_thread(
        lambda: get_limiter().allow(f"chat:{user_id}", **RATE_LIMIT_CHAT)
    )
    if not allowed:
        await sender.send_text("请求过于频繁，请稍后再试。")
        return

    # 2. 先回一句"正在处理"（流式，提升体验），然后异步跑治理链路。
    #    企微 SDK 要求进会话事件 5s 内回欢迎语，但普通文本消息无此限制；
    #    这里用 send_stream 发中间态，最终态 finish=True。
    stream_id = None
    if sender.send_stream is not None:
        # 流式渠道：先发"思考中"
        import uuid

        stream_id = f"stream_{uuid.uuid4().hex[:12]}"
        try:
            await sender.send_stream("正在查阅相关资料...", stream_id, finish=False)
        except Exception:
            stream_id = None  # 流式失败则退化为单条

    # 3. 跑治理链路（同步，放在线程池避免阻塞事件循环）。
    try:
        result = await asyncio.to_thread(_run_sync, user_id, text, msg.channel)
    except Exception:
        logger.exception("治理链路执行失败")
        await sender.send_text("抱歉，处理时出错了，请稍后重试。")
        return

    # 4. 回复结果。流式渠道发最终帧，非流式发单条。
    answer = _format_answer(result.decision, result.answer)
    if stream_id is not None and sender.send_stream is not None:
        try:
            await sender.send_stream(answer, stream_id, finish=True)
            return
        except Exception:
            logger.warning("流式最终帧失败，退化单条回复")
    await sender.send_text(answer)


def _run_sync(user_id: int, question: str, channel: str):
    """在线程池里同步执行治理链路（run_agent 是同步的）。

    LLM 从 DB 激活的 Model 实体解析（与 /chat 一致），在 session 内构造避免多开连接。
    RAG 检索由 run_agent 内部查 KB 路由（不再从外部传 vector_store）。
    """
    with session_scope() as session:
        from bycall.agent.session_store import get_or_create_conversation
        from bycall.governance.models import User
        from bycall.llm.resolver import get_chat_model

        user = session.get(User, user_id)
        if user is None:
            raise RuntimeError(f"用户不存在: id={user_id}")
        # 从激活的 chat Model 实体构造 ChatModel；无激活则给友好错误
        resolved = get_chat_model(session)
        if resolved is None:
            return _no_model_result(user, channel)
        _, chat_model = resolved

        # 多轮会话：按 (user_id, channel) 查/建会话，传入 run_agent 启用历史。
        conv = get_or_create_conversation(session, user_id, channel)

        return run_agent(
            session=session,
            user=user,
            question=question,
            llm=chat_model,
            channel=channel,
            conversation_id=conv.id,
        )


def _no_model_result(user, channel: str):
    """未激活对话模型时的降级结果。"""
    from bycall.agent.runner import AgentResult

    return AgentResult(
        answer="系统尚未配置对话模型，请联系管理员在控制台激活一个模型。",
        decision="error",
        knowledge_scope=[],
        retrieved_chunks=0,
        user_name=user.name,
        role=user.role,
        tool_calls=[],  # 无模型降级路径无工具调用
    )


def _format_answer(decision: str, answer: str) -> str:
    """按决策格式化回复（IM 友好提示）。"""
    if decision == "denied_no_access":
        return answer  # 已是友好提示
    if decision == "denied":
        return answer
    return answer


__all__ = ["handle_im_message"]
