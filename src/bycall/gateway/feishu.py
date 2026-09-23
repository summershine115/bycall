"""飞书（Lark）渠道——自研长连接协议层 + REST 回复/主动推送。

事件传输：WebSocket 长连接（``wss://…``，经 ``POST /callback/ws/endpoint`` 换取），
协议见 :mod:`bycall.gateway.feishu_wire`（对齐官方 lark-oapi 1.7.3，ADR-0015）。
无需公网回调 URL，与企微智能机器人同属「长连接」形态，天然适配内网部署。

本类职责（对齐 :class:`bycall.gateway.wecom.WecomChannel` 的契约）：

1. 收消息：``im.message.receive_v1`` 事件 → ``InboundMessage``。
   - 身份：**union_id 优先**（同一开发者主体下跨 app 稳定），缺失回退 open_id
     （app 维度，同一员工在不同 app 下不同——per-Agent 多 Bot 部署必须同主体，
     见 ADR-0015）。
   - ``chat_id``：**仅群聊填写**（对齐企微语义：InboundMessage.chat_id 是「群聊
     标识」，数据面据此拒绝群聊）。飞书 p2p 也有 ``oc_`` 会话 ID，必须清空，
     否则 im_handler 的群聊守卫会误拒所有私聊——这是最关键的适配点。
   - 文本直取 ``content.text``；post 取富文本段；其余类型降级占位文本（v1 不做
     附件下载，后续经 ``im/v1/messages/{id}/resources`` 补齐）。
2. 发回复：``ReplySender`` 桥接到 REST：
   - ``send_text``：reply 到 message_id；回复目标被撤回/不存在（230011/231003）
     → 回退 create 到 chat_id。
   - ``send_stream``：**卡片伪流式**——首帧发 interactive 卡片，后续 ``PUT`` 更新
     卡片内容（飞书无企微式 WS 流式帧；replace 语义与 im_handler 的累积全文
     完全对齐）。建卡失败降级为 finish 帧一次性文本。
   - ``send_card``：企微模板卡片协议，飞书不实现（None → 数据面跳过澄清卡片）。
3. 主动推送：``send_proactive``——审批结果/异步任务回推。body 为渠道中立 dict
   （wecom 风格 ``{"msgtype": "text"|"markdown", ...}``），翻译成飞书消息。
4. 可观测：生命周期事件（connected/reconnecting/disconnected/error）经日志 +
   OTel span 暴露，回调 ``on_lifecycle`` 供 manager 维护线程安全状态。

依赖：``websockets``（企微 SDK 底层同款，已是运行时依赖）+ ``httpx``。代理语义与
企微相反：飞书域名**不绕过**系统代理（沿用环境代理配置，tests/unit/test_gateway.py
锁定该行为）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from bycall.gateway.base import Channel, InboundMessage, ReplySender
from bycall.gateway.feishu_wire import (
    FEISHU_DOMAIN,
    FRAME_METHOD_CONTROL,
    FRAME_METHOD_DATA,
    HEADER_HANDSHAKE_AUTH_ERRCODE,
    HEADER_HANDSHAKE_MSG,
    HEADER_HANDSHAKE_STATUS,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TYPE,
    MSG_TYPE_EVENT,
    MSG_TYPE_PONG,
    ClientConfig,
    FeishuAuthError,
    FeishuEndpointError,
    FeishuWireError,
    FrameDecodeError,
    ack_frame,
    decode_frame,
    encode_frame,
    fetch_ws_endpoint,
    parse_conn_params,
    ping_frame,
)

logger = logging.getLogger(__name__)

# 回复目标失效错误码（消息被撤回 / 回复目标不存在）→ 回退 create。
_REPLY_TARGET_GONE_CODES = frozenset({230011, 231003})
# tenant_access_token 失效相关码 → 强刷 token 重试一次。
_TOKEN_STALE_CODES = frozenset({99991661, 99991663, 99991668})

# 合包缓存：msg_id → 分片缓冲。超时丢弃（对齐官方 5s）。
_FRAGMENT_TTL_SECONDS = 5.0

# 未订阅/不支持的事件类型：不投递数据面（仍回 ACK，避免服务端重推）。
_EVENT_MESSAGE_RECEIVE = "im.message.receive_v1"

# 非文本消息的占位文本（v1 不下载附件；占位让用户得到诚实反馈而非石沉大海）。
_TYPE_LABELS = {
    "image": "图片",
    "file": "文件",
    "audio": "语音",
    "media": "视频",
    "sticker": "表情",
    "interactive": "卡片",
    "share_chat": "群名片",
    "share_user": "用户名片",
}

# 生命周期事件 → 日志级别（对齐 wecom）。
_LIFECYCLE_LEVEL = {
    "connected": logging.INFO,
    "disconnected": logging.WARNING,
    "reconnecting": logging.WARNING,
    "error": logging.ERROR,
}

# 群聊 @机器人 后文本里的 mention 占位（@_user_1 …），清洗后交给数据面。
_MENTION_PLACEHOLDER_RE = None  # 延迟初始化（re 导入放函数级，保持模块轻）


class FeishuApiError(FeishuWireError):
    """飞书 OpenAPI 业务错误（code != 0）。"""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"飞书 API 错误 code={code} msg={msg}")
        self.code = code
        self.msg = msg


@dataclass
class _SenderContext:
    """构造 ReplySender 所需的渠道侧上下文（从事件里抽出的回复坐标）。"""

    chat_id: str  # 飞书会话 ID（oc_…，p2p 也有——与 InboundMessage.chat_id 语义不同）
    message_id: str  # 被回复的消息 ID（om_…）


def _clean_mention_placeholders(text: str) -> str:
    """去掉群聊消息里的 ``@_user_N`` 占位（v1 群聊虽被拒，清洗保持文本干净）。"""
    import re

    return re.sub(r"@_user_\d+", "", text).strip()


def _extract_text(message_type: str, content_raw: str) -> tuple[str, str] | None:
    """从 ``message.content``（JSON 字符串）抽文本。

    返回 ``(text, message_type)``；``system`` 消息返回 None（不投递）。
    解析失败给占位文本——绝不让一条畸形消息打垮连接。
    """
    if message_type == "system":
        return None
    try:
        content = json.loads(content_raw) if content_raw else {}
    except (TypeError, ValueError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    if message_type == "text":
        return _clean_mention_placeholders(str(content.get("text", ""))), "text"
    if message_type == "post":
        # 富文本：title + 各段 text 元素拼接（post 结构 {title, content[[{tag,text}]]}）
        parts: list[str] = []
        title = str(content.get("title") or "").strip()
        if title:
            parts.append(title)
        blocks = content.get("content") or []
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, list):
                    continue
                for element in block:
                    if isinstance(element, dict):
                        piece = str(element.get("text") or "").strip()
                        if piece and element.get("tag") in ("text", "a", "at"):
                            parts.append(piece)
        return _clean_mention_placeholders("\n".join(p for p in parts if p)), "text"
    label = _TYPE_LABELS.get(message_type, message_type or "未知")
    return f"[收到一条{label}消息，当前暂不支持查看，请以文字描述您的需求]", message_type


def _map_event_to_message(
    event: dict[str, Any], *, agent_id: int | None
) -> tuple[InboundMessage, _SenderContext] | None:
    """``im.message.receive_v1`` 事件 → ``(InboundMessage, 回复上下文)``。

    不可投递（system 消息 / 无发送者）返回 None。
    """
    body = event.get("event") or {}
    sender_id = (body.get("sender") or {}).get("sender_id") or {}
    # union_id 优先（ADR-0015：open_id 是 app 维度，多 app 部署下同人不稳定）。
    im_user_id = str(sender_id.get("union_id") or sender_id.get("open_id") or "")
    message = body.get("message") or {}
    chat_type = str(message.get("chat_type") or "")
    chat_id = str(message.get("chat_id") or "")
    message_id = str(message.get("message_id") or "")
    message_type = str(message.get("message_type") or "text")
    if not im_user_id or not message_id:
        return None
    extracted = _extract_text(message_type, str(message.get("content") or ""))
    if extracted is None:
        return None
    text, mapped_type = extracted
    msg = InboundMessage(
        channel="feishu",
        im_user_id=im_user_id,
        text=text,
        raw_frame=event,
        # 关键适配：InboundMessage.chat_id 是「群聊标识」（企微语义）。飞书 p2p 也有
        # oc_ 会话 ID，必须清空——否则数据面群聊守卫会误拒全部私聊。回复用的真实
        # chat_id 经 _SenderContext 单独携带。
        chat_id=chat_id if chat_type == "group" else "",
        message_type=mapped_type,
        msg_id=message_id,
        agent_id=agent_id,
    )
    return msg, _SenderContext(chat_id=chat_id, message_id=message_id)


def _citations_markdown(msg_item: list | None) -> str:
    """wecom 风格 citation 卡片项 → 飞书 markdown 参考列表（附在最终帧）。

    自带前导空行（与正文隔开）；空列表返回空串，调用方直接拼接。
    """
    if not msg_item:
        return ""
    lines = ["**参考来源**"]
    for item in msg_item:
        if isinstance(item, dict):
            title = str(item.get("title") or "来源")
            url = str(item.get("url") or "")
            lines.append(f"- [{title}]({url})" if url else f"- {title}")
    return "\n\n" + "\n".join(lines)


def _stream_card(content_md: str) -> dict[str, Any]:
    """构造流式占位卡片（interactive，lark_md 渲染 markdown 子集）。"""
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content_md or "…"}}],
    }


class FeishuChannel(Channel):
    """飞书渠道实现（长连接收事件 + REST 发消息）。

    用法（与 WecomChannel 同构）：
        ch = FeishuChannel(app_id=..., app_secret=..., on_lifecycle=cb)
        await ch.start(on_message)   # 阻塞至 stop_async / 致命错误
        ...
        await ch.stop_async()

    连接节奏（服务端 ``ClientConfig`` 权威下发，pong 帧可中途调整）：
    断开后先随机抖动（≤ ReconnectNonce 秒，避免雪崩），再按 ReconnectInterval
    重试；ReconnectCount=-1 无限重连。凭证/握手类致命错误直接放弃（槽位保留供
    stop，is_connected=False，last_error 说明原因）。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str = FEISHU_DOMAIN,
        on_lifecycle: Callable[..., None] | None = None,
        agent_id: int | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        # ADR-0012：本连接绑定的数字员工实例（per-Agent 专属 Bot）。None = 全局默认。
        self._agent_id = agent_id
        self._on_lifecycle = on_lifecycle
        # 服务端下发的连接参数（首次建连后由端点接口 / pong 帧更新）。
        self._config = ClientConfig()
        # 连接状态。
        self._ws: Any | None = None
        self._service_id = 0
        self._device_id = ""
        self._connected = False
        self._stopping = False
        self._closed = asyncio.Event()
        self._ping_task: asyncio.Task | None = None
        self._frame_tasks: set[asyncio.Task] = set()
        self._stop_task: asyncio.Task | None = None
        # 合包缓存：msg_id → (分片列表, 入缓存时间)。
        self._fragments: dict[str, tuple[list[bytes], float]] = {}
        # REST（token 缓存 + 复用连接池；start 时建、stop 时关）。
        self._http: httpx.AsyncClient | None = None
        self._token = ""
        self._token_expire_at = 0.0
        self._last_error = ""
        # 槽位状态展示用（manager 读 _bot_id；飞书维度即 app_id）。
        self._bot_id = app_id

    # ---- 生命周期 ----

    async def start(
        self,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
    ) -> None:
        """建连并常驻收事件，直到 :meth:`stop_async` 或致命错误。

        必须运行在槽位事件循环里（manager 保证）；内部所有等待均可被停连打断。
        """
        self._stopping = False
        self._closed.clear()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        attempt = 0
        nonce_applied = False
        try:
            while not self._stopping:
                try:
                    await self._connect_once()
                except FeishuAuthError as e:
                    # 凭证/握手类致命错误：重试无意义，记录后放弃（槽位保留供 stop）。
                    self._last_error = str(e)
                    self._notify_lifecycle("error", error=str(e))
                    logger.error(
                        "feishu 渠道无法建连（凭证/配置错误），app_id=%s: %s", self._app_id, e
                    )
                    return
                except Exception as e:
                    if self._stopping:
                        break
                    self._last_error = str(e)
                    self._notify_lifecycle("error", error=str(e))
                    await self._teardown_connection()
                else:
                    attempt = 0
                    nonce_applied = False
                    try:
                        await self._recv_loop(on_message)
                    except Exception:
                        if not self._stopping:
                            logger.warning("feishu 接收循环异常退出", exc_info=True)
                    finally:
                        await self._teardown_connection()
                if self._stopping:
                    break
                # 重连判定：超次数上限放弃；否则抖动 + 间隔重试。
                max_attempts = self._config.reconnect_count
                if 0 <= max_attempts <= attempt:
                    self._last_error = f"重连次数耗尽（{max_attempts}）"
                    self._notify_lifecycle("error", error=self._last_error)
                    logger.error("feishu 渠道%s", self._last_error)
                    return
                if not nonce_applied and self._config.reconnect_nonce > 0:
                    # 首次重连随机抖动（对齐官方：打散重连雪崩）。
                    await self._sleep_stoppable(random.random() * self._config.reconnect_nonce)
                    nonce_applied = True
                    if self._stopping:
                        break
                attempt += 1
                self._notify_lifecycle("reconnecting", attempt=attempt)
                await self._sleep_stoppable(max(0.0, float(self._config.reconnect_interval)))
        finally:
            await self._teardown_connection()
            self._closed.set()

    async def _connect_once(self) -> None:
        """换取端点 + 建 WS 连接。致命握手错误转 :class:`FeishuAuthError`。"""
        import websockets

        assert self._http is not None
        url, config = await fetch_ws_endpoint(
            self._http, domain=self._domain, app_id=self._app_id, app_secret=self._app_secret
        )
        self._config = config
        device_id, service_id = parse_conn_params(url)
        try:
            ws = await websockets.connect(url, open_timeout=10, close_timeout=5)
        except Exception as e:
            raise self._map_handshake_error(e) from e
        self._ws = ws
        self._device_id = device_id
        self._service_id = service_id
        self._connected = True
        self._last_error = ""
        self._notify_lifecycle("connected", is_connected=True)
        logger.info(
            "feishu 长连接已建立 app_id=%s device_id=%s（ping=%ds reconnect=%ds/%d）",
            self._app_id,
            device_id,
            config.ping_interval,
            config.reconnect_interval,
            config.reconnect_count,
        )
        self._ping_task = asyncio.create_task(self._ping_loop())

    @staticmethod
    def _map_handshake_error(e: Exception) -> Exception:
        """WS 握手失败分类：凭证/权限类致命，其余按瞬态（可重试）处理。

        错误信息在 upgrade 响应头（handshake-status / handshake-msg /
        handshake-autherrcode），与官方 SDK 的 ``_parse_ws_conn_exception`` 对齐。
        """
        headers = getattr(e, "headers", None)
        if headers is None:
            response = getattr(e, "response", None)
            headers = getattr(response, "headers", None)
        if not headers:
            return FeishuEndpointError(f"WS 握手失败: {e}")
        status = headers.get(HEADER_HANDSHAKE_STATUS)
        if not status:
            return FeishuEndpointError(f"WS 握手失败: {e}")
        msg = headers.get(HEADER_HANDSHAKE_MSG, "")
        try:
            code = int(status)
        except (TypeError, ValueError):
            return FeishuEndpointError(f"WS 握手失败: {status} {msg}")
        if code == 403 or (
            code == 514 and str(headers.get(HEADER_HANDSHAKE_AUTH_ERRCODE, "")) == "1000040350"
        ):
            # 403 禁用 / 514+连接数超限：重试无意义。
            return FeishuAuthError(f"WS 握手被拒: {status} {msg}")
        return FeishuEndpointError(f"WS 握手失败: {status} {msg}")

    async def _recv_loop(
        self, on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]]
    ) -> None:
        """接收循环：DATA 帧派发为独立任务（长回复不阻塞心跳/后续消息）。"""
        ws = self._ws
        while not self._stopping and ws is not None:
            raw = await ws.recv()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            # CONTROL 帧行内处理（廉价）；DATA 帧开任务——处理含完整 agent 回合。
            try:
                frame = decode_frame(raw)
            except FrameDecodeError:
                logger.warning("feishu 收到无法解析的帧（%d 字节），已丢弃", len(raw))
                continue
            if frame.method == FRAME_METHOD_CONTROL:
                self._handle_control(frame)
                continue
            if frame.method != FRAME_METHOD_DATA:
                continue
            task = asyncio.create_task(self._handle_data_frame(frame, on_message))
            self._frame_tasks.add(task)
            task.add_done_callback(self._frame_tasks.discard)

    def _handle_control(self, frame: Any) -> None:
        """控制帧：pong 可能携带新配置（服务端运行中调参）；服务端 ping 忽略。

        协议层心跳（WS 协议帧 ping/pong）由 websockets 库自动应答，与此处
        应用层 CONTROL 心跳是两回事。
        """
        msg_type = frame.header(HEADER_TYPE)
        if msg_type != MSG_TYPE_PONG or not frame.payload:
            return
        try:
            self._config = ClientConfig.from_dict(json.loads(frame.payload.decode("utf-8")))
            logger.info("feishu 连接参数已按服务端下发更新: %s", self._config)
        except (TypeError, ValueError):
            logger.debug("feishu pong 配置解析失败（忽略）", exc_info=True)

    async def _handle_data_frame(
        self,
        frame: Any,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
    ) -> None:
        """单个 DATA 帧：合包 → 派发 → 回 ACK。

        回执语义对齐官方 SDK：card 类回调与**未齐分片不回执**（等最后一片补齐后
        由完整帧回执）；已派发的事件无论内部成败都以 200 回执（派发异常已在内部
        吞掉记日志）——绝不因单条畸形事件触发服务端无限重推。
        """
        if frame.header(HEADER_TYPE) != MSG_TYPE_EVENT:
            return
        payload = self._reassemble(frame)
        if payload is None:
            return
        started = time.monotonic()
        try:
            await self._dispatch_event(payload, on_message)
        except Exception:
            logger.exception(
                "feishu 事件处理异常（message_id=%s）", frame.header(HEADER_MESSAGE_ID)
            )
        try:
            await self._send_bytes(
                encode_frame(ack_frame(frame, biz_rt_ms=int((time.monotonic() - started) * 1000)))
            )
        except Exception:
            # ACK 发不出 = 连接已断，接收循环马上会感知并重连。
            logger.debug("feishu ACK 发送失败（连接可能已断）", exc_info=True)

    def _reassemble(self, frame: Any) -> bytes | None:
        """合包：``sum>1`` 的多片事件按 ``message_id`` 缓冲，齐了才返回。"""
        try:
            total = int(frame.header(HEADER_SUM) or "1")
            seq = int(frame.header(HEADER_SEQ) or "0")
        except (TypeError, ValueError):
            total, seq = 1, 0
        payload = frame.payload or b""
        if total <= 1:
            return payload
        msg_id = frame.header(HEADER_MESSAGE_ID) or uuid.uuid4().hex
        now = time.monotonic()
        # 顺手清理过期分片（官方 5s 窗口，超时丢弃等重推）。
        for key in [
            k for k, (_, ts) in self._fragments.items() if now - ts > _FRAGMENT_TTL_SECONDS
        ]:
            del self._fragments[key]
        buffered = self._fragments.get(msg_id)
        if buffered is None:
            buffered = ([b""] * total, now)
            self._fragments[msg_id] = buffered
        chunks, _ = buffered
        if 0 <= seq < total:
            chunks[seq] = payload
        if all(chunks):
            self._fragments.pop(msg_id, None)
            return b"".join(chunks)
        return None

    async def _dispatch_event(
        self,
        payload: bytes,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
    ) -> None:
        """事件 JSON → 数据面回调。未订阅类型直接忽略（外层仍回 ACK）。"""
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("feishu 事件 payload 非 JSON（%d 字节），已丢弃", len(payload))
            return
        if not isinstance(event, dict):
            return
        header = event.get("header") or {}
        if str(header.get("event_type") or "") != _EVENT_MESSAGE_RECEIVE:
            return
        mapped = _map_event_to_message(event, agent_id=self._agent_id)
        if mapped is None:
            return
        msg, ctx = mapped
        sender = self._build_sender(ctx)
        try:
            await on_message(msg, sender)
        except Exception:
            # 单条消息处理异常不打垮长连接（对齐 wecom）。
            logger.exception("feishu on_message 处理失败，im_user_id=%s", msg.im_user_id)

    async def _ping_loop(self) -> None:
        """应用层心跳：按服务端 ``PingInterval`` 发 CONTROL ping。"""
        try:
            while not self._stopping and self._ws is not None:
                await asyncio.sleep(max(1, self._config.ping_interval))
                if self._stopping or self._ws is None:
                    return
                await self._send_bytes(encode_frame(ping_frame(self._service_id)))
        except asyncio.CancelledError:
            pass
        except Exception:
            # 心跳发不出 = 连接已死；接收循环会感知并触发重连，这里安静退出。
            logger.debug("feishu 心跳发送失败（等待接收循环触发重连）", exc_info=True)

    async def _send_bytes(self, data: bytes) -> None:
        ws = self._ws
        if ws is None:
            raise FeishuWireError("连接未建立，发送失败")
        await ws.send(data)

    async def _sleep_stoppable(self, seconds: float) -> None:
        """可中断睡眠：stop_async 置位后最多 0.2s 内返回。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stopping:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return
            await asyncio.sleep(min(0.2, remain))

    async def _teardown_connection(self) -> None:
        """断开当前连接 + 停心跳/派发任务（幂等；不清 _config——重连沿用）。"""
        self._connected = False
        ws, self._ws = self._ws, None
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("feishu WS 关闭异常（忽略）", exc_info=True)
        if ws is not None and not self._stopping:
            self._notify_lifecycle("disconnected", is_connected=False)

    # ---- ReplySender 桥接 ----

    def _build_sender(self, ctx: _SenderContext) -> ReplySender:
        """构造回复器。流式状态放闭包里（每条入站消息独立，天然隔离并发回合）。"""
        stream_state = {"card_message_id": "", "broken": False}

        async def send_text(content: str) -> None:
            try:
                await self._reply_message(ctx.message_id, content)
            except FeishuApiError as e:
                if e.code in _REPLY_TARGET_GONE_CODES:
                    # 回复目标被撤回/不存在 → 直接发到会话。
                    await self._create_message(ctx.chat_id, content)
                else:
                    raise

        async def send_stream(
            content: str, stream_id: str, finish: bool, msg_item: list | None = None
        ) -> None:
            await self._stream_reply(stream_state, ctx, content, finish, msg_item)

        return ReplySender(
            send_text=send_text,
            send_stream=send_stream,
            channel="feishu",
            raw_frame=None,
        )

    async def _stream_reply(
        self,
        state: dict,
        ctx: _SenderContext,
        content: str,
        finish: bool,
        msg_item: list | None,
    ) -> None:
        """卡片伪流式：首帧建卡，其余帧 PUT 更新（replace 语义，全文覆盖）。

        失败策略：建卡失败 → 标记 broken，后续帧跳过，finish 帧降级 send_text
        一次性发全文（数据面语义保底）；中途更新失败只记日志（下一帧再试）。
        """
        card_id = state["card_message_id"]
        if state["broken"]:
            if finish:
                await self._send_text_via_reply_or_create(ctx, content, msg_item)
            return
        if not card_id:
            try:
                card_id = await self._create_stream_card(ctx, content)
                state["card_message_id"] = card_id
            except Exception:
                state["broken"] = True
                logger.warning("feishu 流式卡片创建失败，finish 帧将降级为文本", exc_info=True)
                if finish:
                    await self._send_text_via_reply_or_create(ctx, content, msg_item)
                return
            return
        final = content + (_citations_markdown(msg_item) if finish else "")
        try:
            await self._update_card(card_id, final)
        except Exception:
            logger.warning("feishu 流式卡片更新失败（card=%s）", card_id, exc_info=True)
            if finish:
                # 最终帧必须可见：更新失败则补一条文本（内容为兜底而非丢失）。
                await self._send_text_via_reply_or_create(ctx, content, msg_item)

    async def _send_text_via_reply_or_create(
        self, ctx: _SenderContext, content: str, msg_item: list | None
    ) -> None:
        """文本兜底：citation 以 markdown 参考列表附尾（对齐数据面非流式降级）。"""
        out = content + _citations_markdown(msg_item)
        try:
            await self._reply_message(ctx.message_id, out)
        except FeishuApiError as e:
            if e.code in _REPLY_TARGET_GONE_CODES:
                await self._create_message(ctx.chat_id, out)
            else:
                raise

    # ---- REST（OpenAPI）----

    async def _tenant_token(self) -> str:
        """tenant_access_token（缓存至过期前 60s，长连接生命周期内复用）。"""
        if self._token and time.monotonic() < self._token_expire_at:
            return self._token
        assert self._http is not None
        resp = await self._http.post(
            f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuApiError(int(data.get("code", -1)), str(data.get("msg", "")))
        self._token = str(data.get("tenant_access_token", ""))
        self._token_expire_at = time.monotonic() + max(60, int(data.get("expire", 3600))) - 60
        return self._token

    async def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """OpenAPI 调用：token 失效自动强刷重试一次；业务码非 0 抛错。"""
        assert self._http is not None
        for refresh in (False, True):
            token = await self._tenant_token()
            resp = await self._http.request(
                method,
                f"{self._domain}{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                data = resp.json()
            except ValueError as e:
                raise FeishuApiError(resp.status_code, f"非 JSON 响应: {resp.text[:120]}") from e
            code = int(data.get("code", -1))
            if code == 0:
                return data.get("data") or {}
            if refresh and code in _TOKEN_STALE_CODES:
                self._token = ""  # 强刷后重试一次
                self._token_expire_at = 0.0
                continue
            raise FeishuApiError(code, str(data.get("msg", "")))
        raise FeishuApiError(-1, "unreachable")

    async def _reply_message(self, message_id: str, content: str) -> None:
        """回复某条消息（文本）。"""
        await self._api(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
                "uuid": uuid.uuid4().hex,
            },
        )

    async def _create_message(
        self, receive_id: str, content: str, *, receive_id_type: str | None = None
    ) -> None:
        """发文本消息到会话/用户（receive_id_type 按 ID 前缀自动识别）。"""
        if receive_id_type is None:
            receive_id_type = _receive_id_type(receive_id)
        await self._api(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
                "uuid": uuid.uuid4().hex,
            },
        )

    async def _create_stream_card(self, ctx: _SenderContext, content: str) -> str:
        """发流式占位卡片（优先 reply 到原消息，撤回则发到会话），返回卡片消息 ID。"""
        card = json.dumps(_stream_card(content), ensure_ascii=False)
        try:
            data = await self._api(
                "POST",
                f"/open-apis/im/v1/messages/{ctx.message_id}/reply",
                json_body={"msg_type": "interactive", "content": card, "uuid": uuid.uuid4().hex},
            )
        except FeishuApiError as e:
            if e.code not in _REPLY_TARGET_GONE_CODES:
                raise
            data = await self._api(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json_body={
                    "receive_id": ctx.chat_id,
                    "msg_type": "interactive",
                    "content": card,
                    "uuid": uuid.uuid4().hex,
                },
            )
        return str((data.get("message") or {}).get("message_id", ""))

    async def _update_card(self, message_id: str, content_md: str) -> None:
        """更新已发卡片内容（飞书官方支持的卡片消息更新接口，伪流式核心）。"""
        await self._api(
            "PUT",
            f"/open-apis/im/v1/messages/{message_id}",
            json_body={
                "msg_type": "interactive",
                "content": json.dumps(_stream_card(content_md), ensure_ascii=False),
            },
        )

    # ---- 主动推送 ----

    async def send_proactive(self, chatid: str, body: dict) -> None:
        """主动推送（审批结果/异步任务回推，manager marshal 到事件循环后执行）。

        ``body`` 是渠道中立 dict（wecom 风格）：``{"msgtype": "text", "text": {...}}``
        / ``{"msgtype": "markdown", "markdown": {...}}``。markdown 以文本消息发出
        （飞书 text 不渲染 markdown 但内容可读；卡片化是后续增强）。
        ``chatid`` 按前缀路由：``oc_`` 会话 / ``ou_`` open_id / ``on_`` union_id。
        """
        msg_type = str(body.get("msgtype") or "")
        if msg_type == "text":
            content = str((body.get("text") or {}).get("content", ""))
        elif msg_type == "markdown":
            content = str((body.get("markdown") or {}).get("content", ""))
        else:
            raise ValueError(f"feishu 主动推送不支持的消息类型: {msg_type}")
        await self._create_message(chatid, content)

    # ---- 状态 ----

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    # ---- 停止 ----

    def stop(self) -> None:
        """同步停止（事件循环线程内直接调用时把异步停连调度到循环，单测用）。

        跨线程停连必须走 :meth:`stop_async`（manager 经 ``run_coroutine_threadsafe``
        marshal 到槽位事件循环执行）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._stopping = True
            return
        self._stop_task = loop.create_task(self.stop_async())

    async def stop_async(self) -> None:
        """停连并等待 ``start()`` 收尾（可安全热重载：真 socket 关闭，无僵尸连接）。"""
        self._stopping = True
        await self._teardown_connection()
        for task in list(self._frame_tasks):
            task.cancel()
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=5)
        except TimeoutError:
            logger.warning("feishu stop_async 等待 start() 收尾超时（可能仍在建连重试中）")
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        logger.info("feishu 渠道已断开（app_id=%s）", self._app_id)

    # ---- 可观测 ----

    def _notify_lifecycle(self, event: str, **attrs: Any) -> None:
        """生命周期事件：日志 + OTel span + 回调 manager（对齐 wecom 同名方法）。"""
        extra = " ".join(f"{k}={v}" for k, v in attrs.items() if k != "is_connected")
        conn = attrs.get("is_connected")
        logger.log(
            _LIFECYCLE_LEVEL.get(event, logging.INFO),
            "feishu 生命周期事件 event=%s is_connected=%s %sapp_id=%s",
            event,
            conn,
            (extra + " ") if extra else "",
            self._app_id,
        )
        try:
            from bycall.observability import get_tracer

            with get_tracer().start_as_current_span(f"feishu.{event}") as span:
                span.set_attribute("channel", "feishu")
                span.set_attribute("event", event)
                if conn is not None:
                    span.set_attribute("is_connected", bool(conn))
                for k, v in attrs.items():
                    if k != "is_connected":
                        span.set_attribute(k, str(v))
        except Exception:
            logger.debug("feishu OTel span 记录失败（event=%s）", event, exc_info=True)
        if self._on_lifecycle is not None:
            try:
                self._on_lifecycle(event, **attrs)
            except Exception:
                logger.debug("feishu on_lifecycle 回调异常（event=%s）", event, exc_info=True)


def _receive_id_type(receive_id: str) -> str:
    """按 ID 前缀识别 receive_id_type（oc_ 会话 / ou_ open_id / on_ union_id）。"""
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("on_"):
        return "union_id"
    return "chat_id"
