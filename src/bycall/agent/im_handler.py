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
    """IM 消息 → 治理链路 → 流式回复。

    每条消息独立 session（IM 是长连接事件流，不复用请求级 session）。
    正常路径：逐 token 流式推送给用户（企微 WebSocket 流式帧）。
    错误路径（未绑定/限流/异常）：单次 send_text 快速返回。
    """
    text = msg.text.strip()
    if not text:
        return

    if len(text) > MAX_IM_QUESTION_LEN:
        await sender.send_text(f"问题过长，请控制在 {MAX_IM_QUESTION_LEN} 字符以内。")
        return

    # 0. 群聊守卫。
    if msg.chat_id:
        await sender.send_text("群聊暂未开放，请在私聊中使用数字员工。")
        logger.info("IM 群聊消息被拒: channel=%s chat_id=%s", msg.channel, msg.chat_id)
        return

    # 1. 身份映射。
    with session_scope() as session:
        user = resolve_user_by_im(session, msg.channel, msg.im_user_id)
        user_id = user.id if user is not None else None

    if user_id is None:
        await sender.send_text(
            f"您还未被授权使用数字员工，请联系管理员绑定您的企微账号。\n\n"
            f"您的企微 UserID：{msg.im_user_id}\n"
            f"请将此 ID 提供给管理员，在「用户管理」中编辑您的账号并填入。"
        )
        logger.info(
            "IM 消息被拒（未绑定用户）: channel=%s im_user_id=%s", msg.channel, msg.im_user_id
        )
        return

    # 限流。
    from bycall.governance.ratelimit import RATE_LIMIT_CHAT, get_limiter

    allowed = await asyncio.to_thread(
        lambda: get_limiter().allow(f"chat:{user_id}", **RATE_LIMIT_CHAT)
    )
    if not allowed:
        await sender.send_text("请求过于频繁，请稍后再试。")
        return

    # 2. 流式执行治理链路。
    import uuid

    stream_id = uuid.uuid4().hex[:12]
    collected: list[str] = []
    can_stream = sender.send_stream is not None

    try:
        async for chunk_text, is_final, result in _run_stream(user_id, text, msg.channel):
            if is_final:
                final_answer = _format_answer(result.decision, result.answer)
                if can_stream:
                    already_sent = "".join(collected)
                    # 未流式覆盖的部分（如 denied 路径全文一次到达）一次性在 finish 帧发送。
                    if not final_answer.startswith(already_sent):
                        await sender.send_stream(final_answer, stream_id, True)
                    else:
                        remainder = final_answer[len(already_sent):]
                        await sender.send_stream(remainder or "", stream_id, True)
                else:
                    await sender.send_text(final_answer or "".join(collected))
                break
            elif chunk_text:
                collected.append(chunk_text)
                if can_stream:
                    await sender.send_stream(chunk_text, stream_id, False)
    except Exception:
        logger.exception("治理链路流式执行失败")
        if can_stream and collected:
            await sender.send_stream("", stream_id, True)
        elif not collected:
            await sender.send_text("抱歉，处理时出错了，请稍后重试。")


async def _run_stream(user_id: int, question: str, channel: str):
    """流式执行治理链路——异步生成器，逐 token yield。

    在生成器内打开 session（保证整个流式期间 session 存活），
    生成器结束时 session 自动关闭（finally 块确保清理）。
    """
    from bycall.agent.runner import run_agent_stream
    from bycall.agent.session_store import get_or_create_conversation
    from bycall.governance.models import User
    from bycall.llm.resolver import get_chat_model

    # 打开 session——流式期间保持存活。用同一 ctx 实例保证 enter/exit 配对。
    ctx = session_scope()
    session: Any = ctx.__enter__()
    try:
        user = session.get(User, user_id)
        if user is None:
            yield f"用户不存在: id={user_id}", True, _no_model_result_static(channel)
            return

        resolved = get_chat_model(session)
        if resolved is None:
            result = _no_model_result_static(channel)
            yield result.answer, True, result
            return
        _, chat_model = resolved

        conv = get_or_create_conversation(session, user_id, channel)

        async for chunk_text, is_final, result in run_agent_stream(
            session=session,
            user=user,
            question=question,
            llm=chat_model,
            channel=channel,
            conversation_id=conv.id,
        ):
            yield chunk_text, is_final, result
    finally:
        ctx.__exit__(None, None, None)


def _no_model_result_static(channel: str = ""):
    """未激活对话模型时的降级结果（静态版本，不依赖 User 对象）。"""
    from bycall.agent.runner import AgentResult

    return AgentResult(
        answer="系统尚未配置对话模型，请联系管理员在控制台激活一个模型。",
        decision="error",
        knowledge_scope=[],
        retrieved_chunks=0,
        user_name="",
        role="",
        tool_calls=[],
    )


__all__ = ["handle_im_message"]


def _format_answer(decision: str, answer: str) -> str:
    """按决策格式化回复（IM 友好提示）。"""
    if decision == "denied_no_access":
        return answer  # 已是友好提示
    if decision == "denied":
        return answer
    return answer


