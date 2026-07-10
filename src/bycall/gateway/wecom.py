"""企微智能机器人渠道（wecom-aibot-python-sdk）。

基于 WebSocket 长连接（``wss://openws.work.weixin.qq.com``），无需公网域名。
SDK 自动处理认证/心跳/重连；本类只负责两件事：

1. 收消息：把 SDK 的 frame → ``InboundMessage``（统一格式）。
2. 发回复：把 ``ReplySender`` 的两个 callable 桥接到 SDK 的 ``reply_stream``。

frame 字段位置（经源码 + 官方文档核实，见 ``_parse_text_frame``）：
- 文本内容：``frame['body']['text']['content']``
- 发送者 userid：``frame['body']['from']['userid']``
- 会话 chatid：``frame['body']['chatid']``（群聊才有；单聊留空）
- req_id（回复透传用）：``frame['headers']['req_id']``，直接把整个 frame 透传给 SDK 即可。

注意：``aibot`` import 放在方法内（lazy），无凭证/未装 SDK 时本模块仍可 import，
``identity`` 等纯逻辑不依赖它。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bycall.gateway.base import Channel, InboundMessage, ReplySender

logger = logging.getLogger(__name__)


def _parse_text_frame(frame: dict[str, Any]) -> tuple[str, str, str]:
    """从企微文本消息 frame 抽取 (im_user_id, text, chat_id)。

    发送者身份优先取 ``body.from.userid``（官方 SDK 文档的 BaseMessage.from.userid）。
    部分实现/转发可能把身份放在 ``headers.from_user.user_id``，作为兜底也查一遍，
    保证不同 SDK 版本/网关转发都能拿到身份。取不到时用空串，由数据面决定是否放行。
    """
    body = frame.get("body", {}) or {}
    text = (body.get("text", {}) or {}).get("content", "")
    from_info = body.get("from", {}) or {}
    im_user_id = from_info.get("userid", "") or from_info.get("user_id", "")
    # 兜底：headers.from_user.user_id
    if not im_user_id:
        headers = frame.get("headers", {}) or {}
        im_user_id = (headers.get("from_user", {}) or {}).get("user_id", "")
    chat_id = body.get("chatid", "") or ""
    return im_user_id, text, chat_id


class WecomChannel(Channel):
    """企微智能机器人渠道实现。

    用法：
        ch = WecomChannel(bot_id=..., secret=...)
        await ch.start(on_message)  # on_message 是 async(msg, sender)
        ...
        ch.stop()
    """

    def __init__(self, bot_id: str, secret: str) -> None:
        self._bot_id = bot_id
        self._secret = secret
        # WSClient 延迟到 start() 才建，避免 import 即连。
        self._client: Any | None = None

    async def start(
        self,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
    ) -> None:
        """建连 + 注册文本/进会话事件回调 + ``await client.connect()``。

        SDK 是 asyncio 事件驱动（pyee.AsyncIOEventEmitter），回调在事件循环里串行派发，
        且 SDK 内部按 req_id 串行化回复队列，因此这里直接 ``await on_message`` 即可
        （简单正确）。若后续 SDK 改成并发派发导致阻塞，再换 ``asyncio.create_task``。
        """
        from aibot import WSClient, WSClientOptions, generate_req_id

        client = WSClient(WSClientOptions(bot_id=self._bot_id, secret=self._secret))
        self._client = client
        channel = self

        @client.on("message.text")
        async def _on_text(frame: dict[str, Any]) -> None:
            im_user_id, text, chat_id = _parse_text_frame(frame)
            msg = InboundMessage(
                channel="wecom",
                im_user_id=im_user_id,
                text=text,
                raw_frame=frame,
                chat_id=chat_id,
                message_type="text",
            )
            sender = channel._build_sender(frame, generate_req_id)
            try:
                await on_message(msg, sender)
            except Exception:
                # 单条消息处理异常不应打垮整个长连接；记录后继续接收下一条。
                logger.exception("wecom on_message 处理失败，im_user_id=%s", im_user_id)

        @client.on("event.enter_chat")
        async def _on_enter_chat(frame: dict[str, Any]) -> None:
            """进会话事件：发一条欢迎语（简单实现，不进 on_message）。"""
            try:
                await client.reply_welcome(
                    frame,
                    {
                        "msgtype": "text",
                        "text": {"content": "您好，我是 Bycall 数字员工，有什么可以帮您？"},
                    },
                )
            except Exception:
                logger.debug("wecom 欢迎语发送失败（可能超时 5s 窗口）", exc_info=True)

        logger.info("wecom 渠道正在建立 WebSocket 连接，bot_id=%s", self._bot_id)
        await client.connect()

    def _build_sender(
        self, frame: dict[str, Any], generate_req_id: Callable[[str], str]
    ) -> ReplySender:
        """构造回复器，把 SDK 的 reply_stream 封成 ``ReplySender`` 的两个 callable。

        - ``send_text``：单条文本——用 ``reply_stream`` 发一条 ``finish=True`` 的流式消息
          （企微没有纯文本 reply，流式单帧即文本回复）。
        - ``send_stream``：流式——``content/stream_id/finish`` 透传，调用方复用 stream_id。
        """
        client = self._client
        assert client is not None  # start() 后才会有 sender

        async def send_text(content: str) -> None:
            stream_id = generate_req_id("stream")
            await client.reply_stream(frame, stream_id, content, finish=True)

        async def send_stream(content: str, stream_id: str, finish: bool) -> None:
            await client.reply_stream(frame, stream_id, content, finish=finish)

        return ReplySender(
            send_text=send_text,
            send_stream=send_stream,
            channel="wecom",
            raw_frame=frame,
        )

    def stop(self) -> None:
        """断开 WebSocket 连接。"""
        if self._client is not None:
            self._client.disconnect()
            self._client = None
            logger.info("wecom 渠道已断开")
