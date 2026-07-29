"""IM 消息处理器 —— 把渠道入站消息接入治理链路。

收到 IM 消息后：
  身份映射(IM user_id → User) → run_agent(治理链路) → 流式回复

未绑定用户（未邀请）→ 友好拒绝；无权知识 → 拒绝（审计落库）；正常 → 流式回复答案。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any

from bycall.db import session_scope
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.gateway.identity import resolve_user_by_im

logger = logging.getLogger(__name__)

MAX_IM_QUESTION_LEN = 4000
# 流式全量刷新节流间隔（秒）：企微是 replace 语义，每帧都发完整全文，帧过密无感知增益
# 且浪费带宽。参考 WeKnora 的 streamFlushInterval，300ms 一帧。测试可 monkeypatch 为 0 关闭。
STREAM_FLUSH_INTERVAL = 0.3

_QUARTER_WORD_RE = re.compile(r"季度|quarter", re.IGNORECASE)
_EXPLICIT_QUARTER_RE = re.compile(
    r"(?<![A-Za-z0-9])Q[1-4](?![A-Za-z0-9])|第[一二三四1234]季度|(?:第一|第二|第三|第四)季度",
    re.IGNORECASE,
)
# 澄清卡片只能处理无副作用的问题；治理、审批或破坏性意图一律留在原链路。
_GOVERNED_INTENT_RE = re.compile(
    r"审批|批准|同意|驳回|授权|权限|删除|移除|清空|销毁|撤销|执行|"
    r"\b(?:approve|approval|reject|authorize|permission|delete|remove|clear|destroy|execute)\b",
    re.IGNORECASE,
)
_QUARTER_CLARIFICATION_PROMPT = "请问您要查询哪个季度？请选择 Q1、Q2、Q3 或 Q4。"


def _needs_quarter_clarification(msg: InboundMessage, text: str) -> bool:
    """仅识别可安全用卡片澄清的普通私聊季度问题。"""
    return (
        msg.message_type == "text"
        and not msg.chat_id
        and not msg.attachments
        and _QUARTER_WORD_RE.search(text) is not None
        and _EXPLICIT_QUARTER_RE.search(text) is None
        and _GOVERNED_INTENT_RE.search(text) is None
    )


def _build_quarter_card() -> dict:
    """构造企微 button_interaction 模板卡片；按钮 key 即回传的季度值。"""
    return {
        "card_type": "button_interaction",
        "main_title": {
            "title": "请选择季度",
            "desc": "请选择要查询的季度，我会继续处理原问题。",
        },
        "button_list": [
            {"text": quarter, "style": 1, "key": quarter} for quarter in ("Q1", "Q2", "Q3", "Q4")
        ],
        "task_id": f"quarter_clarification_{uuid.uuid4().hex}",
    }


def _persist_quarter_clarification(user_id: int, channel: str, question: str) -> None:
    """在发卡前原子保存原问题和澄清提示，使点击继续同一会话。"""
    from bycall.agent.session_store import get_or_create_conversation, save_message

    with session_scope() as session:
        conversation = get_or_create_conversation(session, user_id, channel)
        save_message(session, conversation.id, "user", question)
        save_message(session, conversation.id, "assistant", _QUARTER_CLARIFICATION_PROMPT)
        session.commit()


def _is_unsupported_msg_item_error(error: TypeError) -> bool:
    """仅识别旧渠道不接受 ``msg_item`` 关键字的签名错误。"""
    message = str(error)
    return "unexpected keyword argument" in message and "msg_item" in message


async def handle_im_message(msg: InboundMessage, sender: ReplySender) -> None:
    """IM 消息 → 治理链路 → 流式回复。

    每条消息独立 session（IM 是长连接事件流，不复用请求级 session）。
    正常路径：逐 token 流式推送给用户（企微 WebSocket 流式帧）。
    错误路径（未绑定/限流/异常）：单次 send_text 快速返回。
    """
    # M6 P3：card_action 的所选值始终走原治理链路，不参与澄清检测。
    # 红线 #7：破坏性操作和审批绝不通过 IM 卡片自助完成。
    text = msg.text.strip()
    if not text and not msg.attachments:
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

    # M6 P3 非破坏性季度澄清：先落会话历史再发卡，点击后沿正常路径继续同一会话。
    if sender.send_card is not None and _needs_quarter_clarification(msg, text):
        await asyncio.to_thread(_persist_quarter_clarification, user_id, msg.channel, text)
        await sender.send_card(_build_quarter_card())
        return

    # M6 P1 多模态输入：附件解析文本并入问题（无附件时 no-op）。解析放线程池避免阻塞事件循环。
    if msg.attachments:
        text = await _enrich_question(text, msg.attachments)
    # 2. 流式执行治理链路。
    stream_id = uuid.uuid4().hex[:12]
    # 企微 WebSocket 流式协议是 replace 语义：每帧 content 是"当前应显示的完整文本"，
    # 客户端用它覆盖之前显示的全部内容（非增量追加）。因此这里维护 full_text 累积全文，
    # 每帧发送累积全文，而不是单 token 增量——否则客户端每次只显示最新一个字符（闪烁）。
    full_text = ""
    can_stream = sender.send_stream is not None
    first_chunk = True
    last_flush = time.monotonic()

    # 流式开始前先发"正在思考…"占位，覆盖 RAG 检索 + LLM 首 token 延迟（可达数秒）
    # 期间的空白，提升用户感知（参考 WeKnora）。首个真实 chunk 到来时由 full_text 覆盖占位。
    if can_stream:
        await sender.send_stream("正在思考…", stream_id, False)

    try:
        async for chunk_text, is_final, result in _run_stream(
            user_id, text, msg.channel, im_user_id=msg.im_user_id
        ):
            if is_final:
                final_answer = _format_answer(result.decision, result.answer)
                # M6 P2：RAG 引用来源卡片，附在最终帧（仅确有检索命中时才非空）。
                msg_item = _build_msg_item(getattr(result, "citations", None) or [])
                if can_stream:
                    # finish 帧发完整 final_answer（replace 语义覆盖全文），不受节流影响立即发送。
                    # 空 finish 会让企微客户端清空已显示内容（空白气泡），故必须非空——
                    # final_answer 为空时回退到已累积全文或占位文本保底。
                    finish_content = final_answer or full_text or "正在思考…"
                    if msg_item:
                        try:
                            await sender.send_stream(
                                finish_content, stream_id, True, msg_item=msg_item
                            )
                        except TypeError as e:
                            if _is_unsupported_msg_item_error(e):
                                await sender.send_stream(finish_content, stream_id, True)
                            else:
                                raise
                    else:
                        await sender.send_stream(finish_content, stream_id, True)
                else:
                    # 非流式渠道降级：把来源拼成 Markdown 参考列表附在答案末尾。
                    out = final_answer or full_text
                    if msg_item:
                        out = _append_citation_markdown(out, msg_item)
                    await sender.send_text(out)
                break
            elif chunk_text:
                # 首个真实 chunk 重置 full_text（覆盖"正在思考…"占位），后续累加。
                if first_chunk:
                    full_text = chunk_text
                    first_chunk = False
                else:
                    full_text += chunk_text
                # 节流：避免每个 token 都发一帧（replace 语义下每帧都是全文，帧过密
                # 无感知增益且浪费带宽）。内容累积在 full_text 里不会丢失，下次 flush 一并
                # 发出；is_final 帧不受节流，始终立即发送。
                if can_stream:
                    now = time.monotonic()
                    if now - last_flush >= STREAM_FLUSH_INTERVAL:
                        await sender.send_stream(full_text, stream_id, False)
                        last_flush = now
    except Exception:
        logger.exception("治理链路流式执行失败")
        if can_stream:
            # 异常时用已累积全文（或占位）+ 错误提示作为 finish 帧覆盖显示，
            # 绝不能发空 finish（企微客户端会清空已显示内容 → 空白气泡）。
            base = full_text or "正在思考…"
            error_finish = f"{base}\n\n[处理时出错，请稍后重试]"
            await sender.send_stream(error_finish, stream_id, True)
        else:
            await sender.send_text("抱歉，处理时出错了，请稍后重试。")


async def _run_stream(user_id: int, question: str, channel: str, im_user_id: str = ""):
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
            im_user_id=im_user_id,
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


async def _enrich_question(text: str, attachments: list) -> str:
    """多模态输入（M6 P1）：把附件解析文本并入问题。

    - 文件附件：调 ``parse_document`` 解析为纯文本，以明确分隔的上下文块并入问题，
      同时回填 ``attachment.text``；解析失败降级为失败提示，绝不中断主消息流。
    - 图片附件：视觉模型暂未保证落地，安全起见注入 ``[图片]`` 占位（见 TODO）。

    原始问题保持完整；附件上下文超出 ``MAX_IM_QUESTION_LEN`` 时只截附件部分。
    """
    if not attachments:
        return text

    blocks: list[str] = []
    for att in attachments:
        if att.kind == "file" and att.content:
            block = await asyncio.to_thread(_parse_file_attachment, att)
            if block:
                blocks.append(block)
        elif att.kind == "image":
            # TODO(M6 视觉)：视觉模型落地后，把图片字节喂给多模态 LLM；
            # 当前无可靠视觉能力探测，先注入占位保证不崩。
            blocks.append("[图片]")

    if not blocks:
        return text

    suffix = ("\n\n" if text else "") + "\n\n".join(blocks)
    # 保住原始问题：附件上下文超限时只截附件部分，原文不动。
    budget = MAX_IM_QUESTION_LEN - len(text)
    if budget <= 0:
        return text
    if len(suffix) > budget:
        suffix = suffix[: max(budget - 1, 0)].rstrip() + "…"
    return text + suffix


def _parse_file_attachment(att) -> str:
    """解析单个文件附件为带标注的上下文块；失败降级为失败提示。"""
    from bycall.rag.parsers.engines import parse_document

    name = att.file_name or "附件"
    try:
        result = parse_document(att.content, att.file_name)
    except Exception as e:  # 解析失败绝不中断主消息流
        logger.warning("附件解析失败 file=%s: %s", name, e)
        return f"[附件解析失败：{name}]"
    parsed_text = (getattr(result, "text", "") or "").strip()
    att.text = parsed_text  # 回填，供后续链路/审计复用
    if not parsed_text:
        return f"[附件：{name}（无可读文本）]"
    return f"[附件：{name}]\n{parsed_text}"


def _build_msg_item(citations: list[dict]) -> list[dict]:
    """RAG 引用元数据 → 企微来源卡片（msg_item）。

    形如 ``[{"title", "description", "url"}]``，最多 5 条；按 url 去重（空 url 不参与去重）；
    跳过无标题项（title 先取文档标题，缺失时回退来源标签）。仅确有检索命中时由调用方
    传入非空 citations——无 RAG 命中（纯工具/闲聊）时本函数收到空列表，自然不附卡片。
    """
    seen_urls: set[str] = set()
    items: list[dict] = []
    for c in citations:
        title = str(c.get("title") or c.get("source") or "").strip()
        if not title:
            continue
        url = str(c.get("url") or "").strip()
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        snippet = str(c.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:79].rstrip() + "…"
        items.append({"title": title, "description": snippet or "来源", "url": url})
        if len(items) >= 5:
            break
    return items


def _append_citation_markdown(answer: str, msg_item: list[dict]) -> str:
    """非流式渠道降级：把来源卡片拼成 Markdown 参考列表附在答案末尾。"""
    lines = ["", "**参考来源：**"]
    for i, c in enumerate(msg_item, 1):
        title = c.get("title", "")
        url = c.get("url", "")
        lines.append(f"{i}. [{title}]({url})" if url else f"{i}. {title}")
    return answer.rstrip() + "\n" + "\n".join(lines)
