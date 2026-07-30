"""企微智能机器人渠道（wecom-aibot-python-sdk）。

基于 WebSocket 长连接（``wss://openws.work.weixin.qq.com``），无需公网域名。
SDK 自动处理认证/心跳/重连；本类负责：

1. 收消息：把 SDK 的 frame → ``InboundMessage``（统一格式）。
   - 文本：``message.text``。
   - 图片/文件：``message.image`` / ``message.file`` → 经 ``download_file`` AES 解密后封装为
     ``Attachment``（携带原始字节，agent 层负责解析/视觉），**下载失败也不丢消息**（降级占位附件）。
   - 图文混排：``message.mixed`` → 拆出文本 + 各图片/文件附件。
   - 模板卡片点击：``event.template_card_event`` → ``message_type="card_action"``，交 agent 决策。
2. 发回复：把 ``ReplySender`` 的 callable 桥接到 SDK：
   - ``send_stream`` 支持 ``msg_item``（图文 citation 来源卡片，仅最终帧）。
   - ``send_card`` 发模板卡片（非破坏性澄清）。
3. 主动推送：``send_proactive(chatid, body)`` 复用 SDK ``send_message``（审批结果/异步任务回推）。
4. 连接可观测：生命周期事件（connected/authenticated/disconnected/reconnecting/error）
   经日志 + OTel span 暴露，并回调 ``on_lifecycle`` 让 manager 维护线程安全状态。

frame 字段位置（经源码 + 官方文档核实，见 ``_parse_text_frame``）：
- 文本内容：``frame['body']['text']['content']``
- 发送者 userid：``frame['body']['from']['userid']``
- 会话 chatid：``frame['body']['chatid']``（群聊才有；单聊留空）
- 图片：``frame['body']['image']['fileurl']``（部分实现为 ``url``）+ ``['aeskey']``
- 文件：``frame['body']['file']['fileurl']`` + ``['aeskey']``
- req_id（回复透传用）：``frame['headers']['req_id']``，直接把整个 frame 透传给 SDK 即可。

注意：``aibot`` import 放在方法内（lazy），无凭证/未装 SDK 时本模块仍可 import，
``identity`` 等纯逻辑不依赖它。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bycall.gateway.base import Attachment, Channel, InboundMessage, ReplySender

logger = logging.getLogger(__name__)


def _parse_text_frame(frame: dict[str, Any]) -> tuple[str, str, str]:
    """从企微文本消息 frame 抽取 (im_user_id, text, chat_id)。

    userid 优先取 ``body.from.userid``（官方 BaseMessage.from.userid），
    兜底 ``headers.from_user.user_id``（部分转发实现把身份放这里）。
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


def _extract_userid(frame: dict[str, Any]) -> str:
    """从任意 frame 抽取发送者 userid（body.from.userid 优先，headers 兜底）。"""
    body = frame.get("body", {}) or {}
    from_info = body.get("from", {}) or {}
    im_user_id = from_info.get("userid", "") or from_info.get("user_id", "")
    if not im_user_id:
        headers = frame.get("headers", {}) or {}
        im_user_id = (headers.get("from_user", {}) or {}).get("user_id", "")
    return im_user_id


def _media_url(meta: dict[str, Any]) -> str:
    """取附件下载地址：``fileurl`` 与 ``url`` 两种字段名都兼容（SDK 文档/实现不一致）。"""
    return meta.get("fileurl") or meta.get("url") or ""


def _should_bypass_proxy(uri: str) -> bool:
    """是否对该 WebSocket URI 绕过系统代理（企微 host 一律直连）。

    企微是「国内服务」，本应直连，不应走开发机为外网 LLM 配的 SOCKS/HTTP 代理。
    """
    from urllib.parse import urlparse

    host = (urlparse(str(uri)).hostname or "").lower()
    return host.endswith("work.weixin.qq.com")


def _ensure_wecom_bypasses_proxy() -> None:
    """让企微 WebSocket 长连接绕过系统/环境代理（直连）。

    背景：开发/部署机常开了 SOCKS 代理（macOS 系统代理或 ALL_PROXY 等），给外网 LLM 用。
    websockets>=14 会自动读取该代理，遇 SOCKS 会要求额外装 python-socks，否则抛
        ImportError: python-socks is required to use a SOCKS proxy
    导致 aibot SDK 建连失败、整个 IM 渠道事件循环退出（表现为 IM 无法聊天）。

    aibot SDK 调用 ``websockets.connect()`` 时不传 ``proxy`` 参数，无法从外部关闭；
    装 python-socks 又会把「国内企微」流量绕去外网代理（错误且更慢）。这里用子类
    替换 ``websockets.connect``，仅对企微 host 注入 ``proxy=None`` 走直连，其余 host
    行为完全不变。替换作用于 ``websockets`` 模块属性，SDK 后续重连也命中同一替换，
    天然覆盖重连。参考 WeKnora（Go gorilla/websocket 对 SOCKS 代理静默忽略、企微直连）。
    """
    import websockets

    if getattr(websockets.connect, "_bycall_wecom_no_proxy", False):
        return  # 已替换（幂等）

    _orig_connect = websockets.connect

    class _WecomNoProxyConnect(_orig_connect):
        """对企微 host 强制 ``proxy=None``，其余沿用默认。"""

        _bycall_wecom_no_proxy = True

        def __init__(self, uri: str, *args: Any, **kwargs: Any) -> None:
            if _should_bypass_proxy(uri) and "proxy" not in kwargs:
                kwargs["proxy"] = None
            super().__init__(uri, *args, **kwargs)

    websockets.connect = _WecomNoProxyConnect
    logger.info("已为企微 WebSocket 长连接启用代理绕过（直连，proxy=None）")


# 生命周期事件 → 日志级别映射（connected/authenticated 为 INFO，其余 WARNING/ERROR）。
_LIFECYCLE_LEVEL = {
    "connected": logging.INFO,
    "authenticated": logging.INFO,
    "disconnected": logging.WARNING,
    "reconnecting": logging.WARNING,
    "error": logging.ERROR,
}


class WecomChannel(Channel):
    """企微智能机器人渠道实现。

    用法：
        ch = WecomChannel(bot_id=..., secret=..., on_lifecycle=cb)
        await ch.start(on_message)  # on_message 是 async(msg, sender)
        ...
        ch.stop()

    ``on_lifecycle(event, **kw)`` 由 ``ChannelManager`` 注入，用于把连接状态回传给
    管理器维护的线程安全状态（FastAPI 同步路由跨线程读 status()）。
    """

    def __init__(
        self,
        bot_id: str,
        secret: str,
        *,
        max_reconnect_attempts: int = -1,
        reconnect_interval_ms: int = 1000,
        heartbeat_interval_ms: int = 30000,
        on_lifecycle: Callable[..., None] | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._secret = secret
        # M6 P0：重连可靠性参数。默认 max_reconnect_attempts=-1（无限重连），
        # 避免渠道连续失败 10 次后静默死亡。manager 从 SystemSetting 传入。
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_interval_ms = reconnect_interval_ms
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._on_lifecycle = on_lifecycle
        # WSClient 延迟到 start() 才建，避免 import 即连。
        self._client: Any | None = None
        # 连接状态（生命周期回调里维护，供 last_error / manager 状态读取）。
        self._last_error = ""
        self._reconnect_attempts = 0

    async def start(
        self,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
    ) -> None:
        """建连 + 注册全部消息/事件/生命周期回调 + ``await client.connect()``。

        SDK 是 asyncio 事件驱动（pyee.AsyncIOEventEmitter），回调在事件循环里串行派发，
        且 SDK 内部按 req_id 串行化回复队列，因此这里直接 ``await on_message`` 即可
        （简单正确）。若后续 SDK 改成并发派发导致阻塞，再换 ``asyncio.create_task``。
        """
        from aibot import WSClient, WSClientOptions, generate_req_id

        client = WSClient(
            WSClientOptions(
                bot_id=self._bot_id,
                secret=self._secret,
                max_reconnect_attempts=self._max_reconnect_attempts,
                reconnect_interval=self._reconnect_interval_ms,
                heartbeat_interval=self._heartbeat_interval_ms,
            )
        )
        self._client = client
        channel = self

        # ---- 生命周期：日志 + 状态维护 + OTel span + 回调 manager ----

        @client.on("connected")
        async def _on_connected() -> None:
            # TCP 已连（尚未认证）：清错误、复位重连计数。
            channel._last_error = ""
            channel._reconnect_attempts = 0
            channel._notify_lifecycle("connected", is_connected=True)

        @client.on("authenticated")
        async def _on_authenticated() -> None:
            # 认证通过：连接真正可用，清错误、复位重连计数。
            channel._last_error = ""
            channel._reconnect_attempts = 0
            channel._notify_lifecycle("authenticated", is_connected=True)

        @client.on("disconnected")
        async def _on_disconnected(reason: str) -> None:
            channel._notify_lifecycle("disconnected", is_connected=False, reason=str(reason))

        @client.on("reconnecting")
        async def _on_reconnecting(attempt: int) -> None:
            # 记录当前重连次数（SDK 从 1 开始递增）。
            channel._reconnect_attempts = int(attempt)
            channel._notify_lifecycle("reconnecting", attempt=int(attempt))

        @client.on("error")
        async def _on_error(exc: Exception) -> None:
            channel._last_error = str(exc)
            channel._notify_lifecycle("error", error=str(exc))

        # ---- 文本消息（原有）----

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

        # ---- 多模态：图片/文件（M6 P1）----

        @client.on("message.image")
        async def _on_image(frame: dict[str, Any]) -> None:
            await channel._handle_single_media(frame, "image", client, on_message, generate_req_id)

        @client.on("message.file")
        async def _on_file(frame: dict[str, Any]) -> None:
            await channel._handle_single_media(frame, "file", client, on_message, generate_req_id)

        @client.on("message.mixed")
        async def _on_mixed(frame: dict[str, Any]) -> None:
            await channel._handle_mixed(frame, client, on_message, generate_req_id)

        # ---- 模板卡片点击（M6 P3）----

        @client.on("event.template_card_event")
        async def _on_card_event(frame: dict[str, Any]) -> None:
            await channel._handle_card_event(frame, on_message, generate_req_id)

        # ---- 进会话：发欢迎语（原有，不进 on_message）----

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

        _ensure_wecom_bypasses_proxy()
        logger.info("wecom 渠道正在建立 WebSocket 连接，bot_id=%s", self._bot_id)
        await client.connect()

    # ---- 多模态处理 ----

    async def _handle_single_media(
        self,
        frame: dict[str, Any],
        kind: str,
        client: Any,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
        generate_req_id: Callable[[str], str],
    ) -> None:
        """单图片/文件消息：下载解密 → Attachment → InboundMessage → on_message。

        下载失败也不丢消息：降级为占位附件（kind 已知、content 空），agent 可据此
        礼貌回应用户「附件解析失败」而非静默吞掉。
        """
        body = frame.get("body", {}) or {}
        im_user_id = _extract_userid(frame)
        chat_id = body.get("chatid", "") or ""
        meta = body.get(kind, {}) or {}
        url = _media_url(meta)
        aeskey = meta.get("aeskey") or ""
        attachment = await self._download_attachment(client, kind, url, aeskey)
        msg = InboundMessage(
            channel="wecom",
            im_user_id=im_user_id,
            text="",
            raw_frame=frame,
            chat_id=chat_id,
            message_type=kind,
            attachments=[attachment],
        )
        await self._deliver(msg, on_message, frame, generate_req_id, kind, im_user_id)

    async def _handle_mixed(
        self,
        frame: dict[str, Any],
        client: Any,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
        generate_req_id: Callable[[str], str],
    ) -> None:
        """图文混排消息：拆出文本 + 各图片/文件附件。

        mixed 结构：``body.mixed.msg_item`` 为 ``[{msgtype, text|image|file}, ...]``。
        字段名兼容多种实现（url/fileurl、msgtype/type）。
        """
        body = frame.get("body", {}) or {}
        im_user_id = _extract_userid(frame)
        chat_id = body.get("chatid", "") or ""
        mixed = body.get("mixed", {}) or {}
        items = mixed.get("msg_item") or mixed.get("item_list") or []
        text_parts: list[str] = []
        attachments: list[Attachment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = item.get("msgtype") or item.get("type") or ""
            if itype == "text" or "text" in item:
                t = (item.get("text", {}) or {}).get("content", "")
                if t:
                    text_parts.append(t)
            if "image" in item:
                img = item.get("image", {}) or {}
                url = _media_url(img)
                if url:
                    attachments.append(
                        await self._download_attachment(
                            client, "image", url, img.get("aeskey") or ""
                        )
                    )
            if "file" in item:
                f = item.get("file", {}) or {}
                url = _media_url(f)
                if url:
                    attachments.append(
                        await self._download_attachment(client, "file", url, f.get("aeskey") or "")
                    )
        msg = InboundMessage(
            channel="wecom",
            im_user_id=im_user_id,
            text="\n".join(text_parts),
            raw_frame=frame,
            chat_id=chat_id,
            message_type="mixed",
            attachments=attachments,
        )
        await self._deliver(msg, on_message, frame, generate_req_id, "mixed", im_user_id)

    async def _handle_card_event(
        self,
        frame: dict[str, Any],
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
        generate_req_id: Callable[[str], str],
    ) -> None:
        """模板卡片点击事件：抽取选中值 → ``message_type="card_action"`` 交 agent 决策。

        选中值取 ``event.selected_value``（button/vote interaction）或 ``selected_key``，
        兜底 ``button_key``；agent 据 raw_frame 自行解析 task_id 等完整上下文。
        """
        body = frame.get("body", {}) or {}
        event = body.get("event", {}) or {}
        im_user_id = event.get("userid") or _extract_userid(frame)
        chat_id = body.get("chatid", "") or ""
        text = (
            event.get("selected_value")
            or event.get("selected_key")
            or event.get("button_key")
            or ""
        )
        msg = InboundMessage(
            channel="wecom",
            im_user_id=im_user_id,
            text=str(text),
            raw_frame=frame,
            chat_id=chat_id,
            message_type="card_action",
        )
        await self._deliver(msg, on_message, frame, generate_req_id, "card_action", im_user_id)

    async def _download_attachment(
        self, client: Any, kind: str, url: str, aeskey: str
    ) -> Attachment:
        """下载并 AES 解密单个附件；失败返回占位附件（kind 已知、content 空）。

        不在此解析文件文本——解析与渠道无关（统一走 rag/parsers），放这里会重复。
        """
        if not url:
            # 无下载地址：直接占位（避免 download_file 收到空 URL 抛错）。
            return Attachment(kind=kind, file_name="", content=b"")
        try:
            content, filename = await client.download_file(url, aeskey)
            return Attachment(kind=kind, file_name=filename or "", content=content or b"")
        except Exception:
            logger.warning(
                "wecom 附件下载失败（%s，url=%s），降级为占位附件", kind, url, exc_info=True
            )
            return Attachment(kind=kind, file_name="", content=b"")

    async def _deliver(
        self,
        msg: InboundMessage,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
        frame: dict[str, Any],
        generate_req_id: Callable[[str], str],
        kind: str,
        im_user_id: str,
    ) -> None:
        """构造 sender 并投递 on_message；异常记录但不打垮长连接。"""
        sender = self._build_sender(frame, generate_req_id)
        try:
            await on_message(msg, sender)
        except Exception:
            logger.exception("wecom on_message(%s) 处理失败，im_user_id=%s", kind, im_user_id)

    def _notify_lifecycle(self, event: str, **attrs: Any) -> None:
        """生命周期事件统一处理：日志 + OTel span + 回调 manager。"""
        level = _LIFECYCLE_LEVEL.get(event, logging.INFO)
        # 日志带上 bot_id 与关键属性，便于运维定位。
        extra = " ".join(f"{k}={v}" for k, v in attrs.items() if k != "is_connected")
        conn = attrs.get("is_connected")
        logger.log(
            level,
            "wecom 生命周期事件 event=%s is_connected=%s %sbot_id=%s",
            event,
            conn,
            (extra + " ") if extra else "",
            self._bot_id,
        )
        # OTel span：即使未启用 tracing 也是 no-op span，零开销。
        try:
            from bycall.observability import get_tracer

            with get_tracer().start_as_current_span(f"wecom.{event}") as span:
                span.set_attribute("channel", "wecom")
                span.set_attribute("event", event)
                if conn is not None:
                    span.set_attribute("is_connected", bool(conn))
                for k, v in attrs.items():
                    if k == "is_connected":
                        continue
                    span.set_attribute(k, str(v))
        except Exception:
            logger.debug("wecom OTel span 记录失败（event=%s）", event, exc_info=True)
        # 回调 manager：更新线程安全状态供 status() 读取。
        if self._on_lifecycle is not None:
            try:
                self._on_lifecycle(event, **attrs)
            except Exception:
                logger.debug("wecom on_lifecycle 回调异常（event=%s）", event, exc_info=True)

    def _build_sender(
        self, frame: dict[str, Any], generate_req_id: Callable[[str], str]
    ) -> ReplySender:
        """构造回复器，把 SDK 的 reply 方法封成 ``ReplySender`` 的 callable。

        - ``send_text``：单条文本——用 ``reply_stream`` 发一条 ``finish=True`` 的流式消息
          （企微没有纯文本 reply，流式单帧即文本回复；不带 msg_item）。
        - ``send_stream``：流式——``content/stream_id/finish/msg_item`` 透传，调用方复用 stream_id。
          ``msg_item`` 仅在 ``finish=True`` 时由 SDK 附到最终帧（图文 citation 来源卡片）。
        - ``send_card``：模板卡片（非破坏性澄清，独立于流式文本）。
        """
        from aibot.types import WsCmd

        client = self._client
        assert client is not None  # start() 后才会有 sender
        ws_manager = client._ws_manager
        req_id = frame.get("headers", {}).get("req_id", "")

        async def _send_stream_frame(
            stream_id: str, content: str, finish: bool, msg_item: list | None = None
        ) -> None:
            """直连 WebSocket 发送 stream 帧，不等企微 ack。

            企微对 stream 帧的 ack 延迟（往往随下一条消息的流量才回），而 SDK 的
            ``reply_stream`` → ``send_reply`` 每帧都同步 ``await ack``（无 ack 阻塞 5s、
            延迟 ack 则阻塞到投递），会把流式循环卡死——表现为回复卡住、用户发下一条消息
            时卡住的内容才冲出来。stream 帧在等 ack 之前就已发出，企微按 replace 语义即时
            渲染，丢单帧由下一帧覆盖、无需逐帧确认。故绕开 SDK 的 ack 等待，直接经
            ``ws_manager.send`` 发出（与 SDK ``send_reply`` 内部发帧同一路径，仅去掉 ack 等待）。
            """
            stream_body: dict[str, Any] = {"id": stream_id, "finish": finish, "content": content}
            if finish and msg_item:
                stream_body["msg_item"] = msg_item
            await ws_manager.send(
                {
                    "cmd": WsCmd.RESPONSE,
                    "headers": {"req_id": req_id},
                    "body": {"msgtype": "stream", "stream": stream_body},
                }
            )

        async def send_text(content: str) -> None:
            stream_id = generate_req_id("stream")
            await _send_stream_frame(stream_id, content, finish=True)

        async def send_stream(
            content: str, stream_id: str, finish: bool, msg_item: list | None = None
        ) -> None:
            await _send_stream_frame(stream_id, content, finish=finish, msg_item=msg_item)

        async def send_card(card: dict) -> None:
            await client.reply_template_card(frame, card)

        return ReplySender(
            send_text=send_text,
            send_stream=send_stream,
            send_card=send_card,
            channel="wecom",
            raw_frame=frame,
        )

    @property
    def is_connected(self) -> bool:
        """真实连接状态（取 SDK 的 is_connected，最权威）。"""
        return bool(self._client and self._client.is_connected)

    @property
    def last_error(self) -> str:
        return self._last_error

    async def send_proactive(self, chatid: str, body: dict) -> None:
        """主动推送（不依赖某条入站 frame）。需在已连接状态下调用。

        manager 把它 marshal 到 IM 事件循环后执行；chatid 为单聊 userid 或群聊 chatid。
        """
        assert self._client is not None and self._client.is_connected, (
            "wecom 渠道未连接，无法主动推送"
        )
        await self._client.send_message(chatid, body)

    def stop(self) -> None:
        """同步断开（保留给事件循环线程内的直接调用，如单测）。

        注意：SDK 的 ``disconnect()`` 内部用 ``asyncio.ensure_future`` 安排真正的 WS
        关闭，**必须在事件循环所在线程调用**。跨线程（如 admin/FastAPI 工作线程）
        断开必须走 :meth:`stop_async`，由 manager 经 ``run_coroutine_threadsafe`` marshal
        到 IM 事件循环线程执行——否则在工作线程里抛 ``no current event loop``，WS 永不关闭，
        旧连接残留导致企微单连接策略下重连后收不到消息（用户表现为「切换渠道后不回复」）。
        """
        if self._client is not None:
            self._client.disconnect()
            self._client = None
            logger.info("wecom 渠道已断开")

    async def stop_async(self) -> None:
        """在 IM 事件循环线程里断开连接，并等待 WS 真正关闭。

        必须由 manager 经 ``run_coroutine_threadsafe`` 在 IM 事件循环线程执行：
        ``disconnect()`` 只是把 ``_async_disconnect`` 调度到 loop（此时 loop 正在运行，
        ``ensure_future`` 才能正确解析），真正的 ``ws.close()`` 异步完成。这里轮询
        ``is_connected`` 等到 socket 关闭（SDK close_timeout=5s），保证 manager 停 loop 前
        旧连接已彻底断开，不残留僵尸连接与重连后的新连接争抢企微单连接。
        """
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            logger.debug("wecom disconnect 调度异常", exc_info=True)
        deadline = time.monotonic() + 6.0
        while getattr(client, "is_connected", False) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        logger.info("wecom 渠道已断开")
