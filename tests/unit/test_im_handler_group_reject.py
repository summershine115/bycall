"""群聊消息拒绝测试（P0-2：群聊绕过私聊隔离）。

验证 chat_id 非空的消息被直接拒绝：
- 不调用 run_agent（不进治理链路、不查知识库、不调 LLM）
- 不创建/查询会话（不串私聊历史）
- 不做身份映射（不泄露用户是否绑定）
- 回复友好拒绝语
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bycall.agent.im_handler import handle_im_message
from bycall.gateway.base import InboundMessage, ReplySender


def _make_sender() -> MagicMock:
    """构造一个 mock ReplySender，send_text 是 AsyncMock。"""
    return ReplySender(
        send_text=AsyncMock(),
        send_stream=None,
        channel="wecom",
        raw_frame={},
    )


def test_group_message_is_rejected():
    """chat_id 非空 → 拒绝 + 回复友好语 + 不进治理链路。"""
    msg = InboundMessage(
        channel="wecom",
        im_user_id="user_a",
        text="帮我查一下销售数据",
        raw_frame={},
        chat_id="group_xyz",
        message_type="text",
    )
    sender = _make_sender()

    # patch run_agent 确保它不被调用
    with (
        patch("bycall.agent.im_handler.run_agent") as mock_run,
        patch("bycall.agent.im_handler.session_scope"),
        patch("bycall.agent.im_handler.resolve_user_by_im") as mock_resolve,
    ):
        asyncio.run(handle_im_message(msg, sender))

    # 回复了拒绝语
    sender.send_text.assert_awaited_once()
    reply = sender.send_text.call_args.args[0]
    assert "群聊" in reply

    # 没进治理链路
    mock_run.assert_not_called()

    # 没做身份映射（拒绝发生在身份映射之前，不泄露绑定状态）
    mock_resolve.assert_not_called()


def test_private_message_passes_through():
    """chat_id 为空（私聊）→ 正常进入治理链路（不被群聊守卫拦截）。

    这里只验证不被守卫拦截——run_agent 会因未绑定用户在身份映射阶段拒绝，
    但那不是群聊守卫的职责。
    """
    msg = InboundMessage(
        channel="wecom",
        im_user_id="user_a",
        text="帮我查一下销售数据",
        raw_frame={},
        chat_id="",  # 私聊
        message_type="text",
    )
    sender = _make_sender()

    with (
        patch("bycall.agent.im_handler.session_scope") as mock_scope,
        patch(
            "bycall.agent.im_handler.resolve_user_by_im",
            return_value=None,  # 未绑定 → 友好拒绝
        ),
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=None)
        asyncio.run(handle_im_message(msg, sender))

    # 私聊不被群聊守卫拦：会走到身份映射，未绑定 → 友好拒绝（非"群聊"措辞）
    sender.send_text.assert_awaited_once()
    reply = sender.send_text.call_args.args[0]
    assert "群聊" not in reply
    assert "授权" in reply or "邀请" in reply  # 未绑定拒绝语


def test_group_empty_text_not_processed():
    """群聊空文本 → 直接 return（不回复，不拒绝）——与私聊空文本一致。"""
    msg = InboundMessage(
        channel="wecom",
        im_user_id="user_a",
        text="   ",  # 空白
        raw_frame={},
        chat_id="group_xyz",
        message_type="text",
    )
    sender = _make_sender()

    asyncio.run(handle_im_message(msg, sender))

    # 空文本先于群聊守卫拦截，不回复任何消息
    sender.send_text.assert_not_awaited()


def test_overlong_private_message_rejected_before_identity_lookup():
    msg = InboundMessage(
        channel="wecom",
        im_user_id="user_a",
        text="x" * 4001,
        raw_frame={},
        chat_id="",
    )
    sender = _make_sender()
    with patch("bycall.agent.im_handler.resolve_user_by_im") as mock_resolve:
        asyncio.run(handle_im_message(msg, sender))
    sender.send_text.assert_awaited_once()
    assert "过长" in sender.send_text.call_args.args[0]
    mock_resolve.assert_not_called()


def test_private_message_rate_limit_shared_with_rest():
    msg = InboundMessage(
        channel="wecom",
        im_user_id="user_a",
        text="正常问题",
        raw_frame={},
        chat_id="",
    )
    sender = _make_sender()
    user = MagicMock(id=123)
    limiter = MagicMock()
    limiter.allow.return_value = False

    with (
        patch("bycall.agent.im_handler.session_scope") as mock_scope,
        patch("bycall.agent.im_handler.resolve_user_by_im", return_value=user),
        patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=None)
        asyncio.run(handle_im_message(msg, sender))

    limiter.allow.assert_called_once_with("chat:123", limit=30, window=60)
    assert "频繁" in sender.send_text.call_args.args[0]
