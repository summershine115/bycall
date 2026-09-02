"""M6 企微渠道能力增强单测。

覆盖 M6 验收点（不真实建连，全程 mock ``aibot.WSClient``）：
1. ``WSClientOptions`` 按设置构造 ``max_reconnect_attempts=-1``（无限重连，非 SDK 默认 10）。
2. 生命周期事件（disconnected/reconnecting/error）→ manager.status() 状态更新。
3. ``send_proactive`` 未连接时返回 False（不抛异常）；已连接时 marshal 调 ``send_message``。
4. 图片/文件 frame → ``download_file(fileurl, aeskey)`` → ``InboundMessage.attachments``；
   下载失败仍投递消息（不丢）。
5. ``send_stream(..., msg_item=...)`` → ``reply_stream(..., msg_item=...)``；
   ``send_card(...)`` → ``reply_template_card(frame, ...)``
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings

# ============ 共用：临时 DB + 模拟 WSClient ============


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含 admin/member/默认提示词/im.* 设置种子）。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, session_scope, set_engine

    set_engine(
        create_engine(
            f"sqlite:///{tmp_path}/test.db",
            connect_args={"check_same_thread": False},
        )
    )
    init_db()
    with session_scope() as s:
        yield s


class _FakeWsManager:
    """模拟 aibot WsConnectionManager：捕获直连发送的帧（send_stream/send_text 走此路径）。"""

    def __init__(self) -> None:
        self.sent_frames: list = []

    async def send(self, frame) -> None:
        self.sent_frames.append(frame)


class _FakeWSClient:
    """模拟 aibot.WSClient：捕获注册的回调 + 记录 reply/download/send 调用。"""

    def __init__(self, options) -> None:
        self.options = options
        self.handlers: dict[str, list] = {}
        self._connected = False
        self._ws_manager = _FakeWsManager()  # 直连发送（send_stream/send_text）捕获帧
        self.reply_calls: list = []  # reply_stream
        self.card_calls: list = []  # reply_template_card
        self.download_calls: list = []  # download_file
        self.send_calls: list = []  # send_message
        self.welcome_calls: list = []
        self.download_result = (b"", None)
        self.download_exc: Exception | None = None

    def on(self, event, f=None):
        # 兼容装饰器用法 ``@client.on("x")`` 与直接注册 ``client.on("x", f)``。
        if f is None:

            def deco(func):
                self.handlers.setdefault(event, []).append(func)
                return func

            return deco
        self.handlers.setdefault(event, []).append(f)
        return f

    async def connect(self):
        self._connected = True
        return self

    def disconnect(self):
        self._connected = False

    @property
    def is_connected(self):
        return self._connected

    async def reply_stream(
        self, frame, stream_id, content, finish=False, msg_item=None, feedback=None
    ):
        self.reply_calls.append((frame, stream_id, content, finish, msg_item))

    async def reply_template_card(self, frame, template_card, feedback=None):
        self.card_calls.append((frame, template_card))

    async def reply_welcome(self, frame, body):
        self.welcome_calls.append((frame, body))

    async def download_file(self, url, aes_key=None):
        self.download_calls.append((url, aes_key))
        if self.download_exc:
            raise self.download_exc
        return self.download_result

    async def send_message(self, chatid, body):
        self.send_calls.append((chatid, body))


def _patch_wsclient(monkeypatch) -> None:
    """把 aibot.WSClient 替换为 _FakeWSClient（start() 内 lazy import 命中替换）。"""
    import aibot

    monkeypatch.setattr(aibot, "WSClient", _FakeWSClient)


async def _noop_on_message(msg, sender):
    """空消息回调。"""


def _emit(fake, event, *args):
    """返回已注册的事件处理器列表（测试中直接 await 模拟「SDK 派发事件」）。"""
    return fake.handlers.get(event, [])


# ============ 1. WSClientOptions 无限重连（来自设置，非 SDK 默认 10）============


@pytest.mark.asyncio
async def test_options_built_with_unlimited_reconnect_from_settings(db_session, monkeypatch):
    """im.wecom_max_reconnect_attempts 种子为 -1 → 构造的 WSClientOptions 也是 -1（非 SDK 默认 10）。"""
    from bycall.governance.settings_store import set_setting_value

    set_setting_value(db_session, "im.enable", "true")
    set_setting_value(db_session, "im.wecom_bot_id", "bid")
    set_setting_value(db_session, "im.wecom_bot_secret", "sec")
    db_session.commit()

    _patch_wsclient(monkeypatch)
    from bycall.gateway.manager import ChannelManager

    mgr = ChannelManager()
    channel, err = mgr._build_channel_from_settings()
    assert channel is not None, err
    await channel.start(_noop_on_message)

    opts = channel._client.options
    assert opts.max_reconnect_attempts == -1
    assert opts.bot_id == "bid"
    channel.stop()


@pytest.mark.asyncio
async def test_options_pass_through_reconnect_and_heartbeat(monkeypatch):
    """reconnect_interval / heartbeat_interval 透传到 WSClientOptions（单位毫秒）。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel(
        "b",
        "s",
        max_reconnect_attempts=-1,
        reconnect_interval_ms=2000,
        heartbeat_interval_ms=15000,
    )
    await ch.start(_noop_on_message)
    opts = ch._client.options
    assert opts.reconnect_interval == 2000
    assert opts.heartbeat_interval == 15000
    ch.stop()


# ============ 2. 生命周期事件 → manager.status() 状态 ============


@pytest.mark.asyncio
async def test_lifecycle_events_update_manager_state(monkeypatch):
    """disconnected/reconnecting/error/authenticated 事件经 on_lifecycle 更新槽位状态。

    ADR-0012 多槽位后状态挂在槽位上：顶层 status() 取全局槽位，``channels`` 逐槽位明细。
    """
    _patch_wsclient(monkeypatch)
    from bycall.gateway.manager import GLOBAL_SLOT_KEY, ChannelManager, _ChannelSlot

    mgr = ChannelManager()
    ch = mgr._new_wecom_channel("b", "s", agent_id=None)  # on_lifecycle 已绑定全局槽位 key
    mgr._slots[GLOBAL_SLOT_KEY] = _ChannelSlot(GLOBAL_SLOT_KEY, ch, None, "b")
    await ch.start(_noop_on_message)
    fake = ch._client

    # error → last_error + last_event
    await _emit(fake, "error")[0](Exception("boom"))
    st = mgr.status()
    assert st["last_event"] == "error"
    assert st["last_error"] == "boom"
    assert st["is_connected"] is False

    # reconnecting → reconnect_attempts
    await _emit(fake, "reconnecting")[0](3)
    assert mgr.status()["reconnect_attempts"] == 3

    # authenticated → 连接可用、清错误、复位计数
    await _emit(fake, "authenticated")[0]()
    st = mgr.status()
    assert st["is_connected"] is True
    assert st["last_error"] == ""
    assert st["reconnect_attempts"] == 0

    # disconnected → 断开
    await _emit(fake, "disconnected")[0]("peer closed")
    assert mgr.status()["is_connected"] is False

    # channels 明细：全局槽位 scope=global、无 agent_id
    channels = mgr.status()["channels"]
    assert len(channels) == 1
    assert channels[0]["scope"] == "global"
    assert channels[0]["agent_id"] is None
    assert channels[0]["bot_id"] == "b"
    ch.stop()


# ============ 3. send_proactive ============


def test_send_proactive_returns_false_when_not_running():
    """未运行/未连接时 send_proactive 返回 False，绝不抛异常（审批流程不能断）。"""
    from bycall.gateway.manager import ChannelManager

    mgr = ChannelManager()
    assert mgr.send_proactive("u1", {"msgtype": "markdown", "markdown": {"content": "x"}}) is False


@pytest.mark.asyncio
async def test_channel_send_proactive_calls_send_message_when_connected(monkeypatch):
    """渠道已连接时 send_proactive 直接调 SDK send_message(chatid, body)。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel("b", "s")
    await ch.start(_noop_on_message)
    assert ch.is_connected is True

    body = {"msgtype": "markdown", "markdown": {"content": "审批通过"}}
    await ch.send_proactive("user_001", body)
    assert ch._client.send_calls == [("user_001", body)]
    ch.stop()


@pytest.mark.asyncio
async def test_channel_send_proactive_raises_when_not_connected(monkeypatch):
    """渠道未连接时 send_proactive 断言失败（manager 层负责拦截，channel 层显式报错）。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel("b", "s")
    await ch.start(_noop_on_message)
    ch._client._connected = False  # 模拟断开
    with pytest.raises(AssertionError):
        await ch.send_proactive("u1", {})


# ============ 4. 多模态：图片/文件下载 → Attachment ============


def _image_frame() -> dict:
    return {
        "headers": {"req_id": "r1"},
        "body": {
            "from": {"userid": "u1"},
            "chatid": "",
            "image": {"fileurl": "http://wecom/img", "aeskey": "k1"},
        },
    }


def _file_frame() -> dict:
    return {
        "headers": {"req_id": "r2"},
        "body": {
            "from": {"userid": "u2"},
            "chatid": "grp",
            "file": {"fileurl": "http://wecom/file", "aeskey": "k2"},
        },
    }


def _mixed_frame() -> dict:
    return {
        "headers": {"req_id": "r3"},
        "body": {
            "from": {"userid": "u3"},
            "chatid": "",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "看这张图"}},
                    {"msgtype": "image", "image": {"fileurl": "http://wecom/m", "aeskey": "k3"}},
                ]
            },
        },
    }


@pytest.mark.asyncio
async def test_inbound_image_frame_downloads_to_attachment(monkeypatch):
    """image frame → download_file(fileurl, aeskey) → Attachment(kind=image, content=bytes)。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    received: list = []

    async def on_message(msg, sender):
        received.append(msg)

    ch = WecomChannel("b", "s")
    await ch.start(on_message)
    ch._client.download_result = (b"img-bytes", "x.png")

    await _emit(ch._client, "message.image")[0](_image_frame())

    assert len(received) == 1
    msg = received[0]
    assert msg.channel == "wecom"
    assert msg.im_user_id == "u1"
    assert msg.message_type == "image"
    assert ch._client.download_calls == [("http://wecom/img", "k1")]
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.kind == "image"
    assert att.content == b"img-bytes"
    assert att.file_name == "x.png"
    ch.stop()


@pytest.mark.asyncio
async def test_inbound_file_frame_downloads_to_attachment(monkeypatch):
    """file frame → download_file → Attachment(kind=file)。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    received: list = []

    async def on_message(msg, sender):
        received.append(msg)

    ch = WecomChannel("b", "s")
    await ch.start(on_message)
    ch._client.download_result = (b"file-bytes", "report.pdf")

    await _emit(ch._client, "message.file")[0](_file_frame())

    msg = received[0]
    assert msg.message_type == "file"
    assert msg.chat_id == "grp"
    assert ch._client.download_calls == [("http://wecom/file", "k2")]
    assert msg.attachments[0].kind == "file"
    assert msg.attachments[0].content == b"file-bytes"
    assert msg.attachments[0].file_name == "report.pdf"
    ch.stop()


@pytest.mark.asyncio
async def test_mixed_frame_extracts_text_and_image(monkeypatch):
    """mixed frame → 文本拼接 + 图片附件。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    received: list = []

    async def on_message(msg, sender):
        received.append(msg)

    ch = WecomChannel("b", "s")
    await ch.start(on_message)
    ch._client.download_result = (b"mix-img", None)

    await _emit(ch._client, "message.mixed")[0](_mixed_frame())

    msg = received[0]
    assert msg.message_type == "mixed"
    assert msg.text == "看这张图"
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == "image"
    assert msg.attachments[0].content == b"mix-img"
    ch.stop()


@pytest.mark.asyncio
async def test_download_failure_still_delivers_message(monkeypatch):
    """download_file 抛异常 → 仍投递 InboundMessage（占位附件，content 空），绝不丢消息。"""
    _patch_wsclient(monkeypatch)
    from bycall.gateway.wecom import WecomChannel

    received: list = []

    async def on_message(msg, sender):
        received.append(msg)

    ch = WecomChannel("b", "s")
    await ch.start(on_message)
    ch._client.download_exc = RuntimeError("decrypt failed")

    await _emit(ch._client, "message.image")[0](_image_frame())

    # 消息照常投递，附件降级为占位（kind 已知、content 空）。
    assert len(received) == 1
    msg = received[0]
    assert msg.message_type == "image"
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == "image"
    assert msg.attachments[0].content == b""
    ch.stop()


# ============ 5. send_stream msg_item + send_card ============


@pytest.mark.asyncio
async def test_send_stream_passes_msg_item(monkeypatch):
    """send_stream 直连发送 stream 帧：content/finish/msg_item 透传到帧 body.stream。"""
    _patch_wsclient(monkeypatch)
    from aibot import generate_req_id

    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel("b", "s")
    await ch.start(_noop_on_message)
    frame = {"headers": {"req_id": "r1"}}
    sender = ch._build_sender(frame, generate_req_id)

    await sender.send_stream("部分内容", "sid-1", finish=False)
    await sender.send_stream("最终内容", "sid-1", finish=True, msg_item=[{"title": "来源"}])

    frames = ch._client._ws_manager.sent_frames
    # 帧结构：cmd=req, headers.req_id 透传，body.stream 含 id/finish/content。
    assert frames[0]["cmd"] == "aibot_respond_msg"
    assert frames[0]["headers"]["req_id"] == "r1"
    assert frames[0]["body"]["stream"]["content"] == "部分内容"
    assert frames[0]["body"]["stream"]["finish"] is False
    assert "msg_item" not in frames[0]["body"]["stream"]  # 仅 finish 帧带 msg_item
    assert frames[1]["body"]["stream"]["content"] == "最终内容"
    assert frames[1]["body"]["stream"]["finish"] is True
    assert frames[1]["body"]["stream"]["msg_item"] == [{"title": "来源"}]
    ch.stop()


@pytest.mark.asyncio
async def test_send_text_has_no_msg_item(monkeypatch):
    """send_text 是单条 finish 帧，不带 msg_item（citation 只走 send_stream）。"""
    _patch_wsclient(monkeypatch)
    from aibot import generate_req_id

    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel("b", "s")
    await ch.start(_noop_on_message)
    sender = ch._build_sender({"headers": {"req_id": "r1"}}, generate_req_id)
    await sender.send_text("hi")
    frame = ch._client._ws_manager.sent_frames[0]
    assert frame["body"]["stream"]["finish"] is True  # send_text 是单条 finish 帧
    assert "msg_item" not in frame["body"]["stream"]  # 不带 msg_item（citation 只走 send_stream）
    ch.stop()


@pytest.mark.asyncio
async def test_send_stream_bypasses_per_frame_ack_wait(monkeypatch):
    """回归：stream 帧直连 WS 发送、不等企微逐帧 ack。

    企微对 stream 帧 ack 延迟（往往随下一条消息的流量才回），SDK ``reply_stream`` 每帧
    ``await ack`` 会把流式循环卡死——表现为回复卡住、用户发下一条消息时卡住的内容才冲出来。
    send_stream 必须绕开 reply_stream、经 ``ws_manager.send`` 直发。本测试验证 send_stream
    不触碰 reply_stream（reply_calls 为空），帧全部经 ws_manager 直发。
    """
    _patch_wsclient(monkeypatch)
    from aibot import generate_req_id

    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel("b", "s")
    await ch.start(_noop_on_message)
    sender = ch._build_sender({"headers": {"req_id": "r1"}}, generate_req_id)

    await sender.send_stream("tok1", "sid", finish=False)
    await sender.send_stream("tok2", "sid", finish=False)
    await sender.send_stream("完整答案", "sid", finish=True)

    # 直连发送：帧进 ws_manager，不走 reply_stream（否则会被逐帧 ack 卡死）。
    assert ch._client.reply_calls == [], "send_stream 不应走 reply_stream（会逐帧等 ack 卡死）"
    frames = ch._client._ws_manager.sent_frames
    assert len(frames) == 3
    assert frames[2]["body"]["stream"]["content"] == "完整答案"
    assert frames[2]["body"]["stream"]["finish"] is True
    ch.stop()


@pytest.mark.asyncio
async def test_send_card_calls_reply_template_card(monkeypatch):
    """send_card(dict) → reply_template_card(frame, dict)。"""
    _patch_wsclient(monkeypatch)
    from aibot import generate_req_id

    from bycall.gateway.wecom import WecomChannel

    ch = WecomChannel("b", "s")
    await ch.start(_noop_on_message)
    frame = {"headers": {"req_id": "r1"}}
    sender = ch._build_sender(frame, generate_req_id)
    card = {"card_type": "button_interaction", "task_id": "t1"}
    await sender.send_card(card)
    assert ch._client.card_calls == [(frame, card)]
    ch.stop()


# ============ 6. 热重载跨线程断开（回归：切换 IM 渠道后不回复）============


def test_stop_slot_disconnects_on_loop_thread_cross_thread():
    """_stop_slot_locked 必须在 IM 事件循环线程断开连接，绝不在工作线程调同步 stop()。

    回归「切换 IM 渠道后 Agent 不回复」：admin/FastAPI 工作线程无运行中的事件循环，
    若跨线程直接调 SDK disconnect（内部 asyncio.ensure_future）会抛 no current event loop，
    WS 永不关闭 → 企微单连接策略下重连后的新连接收不到消息。修复后 manager 经
    run_coroutine_threadsafe 把断开 marshal 到 IM 事件循环线程执行并等真正关闭。
    """
    import asyncio
    import threading
    import time

    from bycall.gateway.manager import GLOBAL_SLOT_KEY, ChannelManager

    ran_on = []  # stop_async 实际执行的线程名
    stop_offloop = []  # 若同步 stop 被跨线程调用则记录（应保持空）

    class FakeChannel:
        """模拟 SDK 行为：同步 stop 在无运行 loop 的线程里抛错（ensure_future 语义）。"""

        def __init__(self) -> None:
            self._connected = True

        async def start(self, on_message) -> None:
            return  # SDK connect() 建连后返回，靠后台任务维持长连接

        def stop(self) -> None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                stop_offloop.append(threading.current_thread().name)
                raise RuntimeError("There is no current event loop in thread") from None
            self._connected = False

        async def stop_async(self) -> None:
            ran_on.append(threading.current_thread().name)
            self._connected = False

        @property
        def is_connected(self) -> bool:
            return self._connected

        @property
        def last_error(self) -> str:
            return ""

    mgr = ChannelManager()
    ch = FakeChannel()
    mgr._start_slot_locked(GLOBAL_SLOT_KEY, ch, agent_id=None)  # 独立 daemon 线程起事件循环

    slot = mgr._slots[GLOBAL_SLOT_KEY]
    deadline = time.monotonic() + 2.0
    while slot.loop is None or not slot.loop.is_running():
        assert time.monotonic() < deadline, "IM 事件循环未启动"
        time.sleep(0.01)

    # 从无事件循环的工作线程调 _stop_slot_locked（模拟 admin/FastAPI 同步路由线程）
    err: list = []

    def caller() -> None:
        try:
            mgr._stop_slot_locked(GLOBAL_SLOT_KEY)
        except Exception as e:
            err.append(e)

    t = threading.Thread(target=caller, name="admin-worker")
    t.start()
    t.join(timeout=15)

    assert not err, f"_stop_slot_locked 抛异常: {err}"
    assert ran_on, "stop_async 未在 IM 事件循环线程执行（连接未真正关闭）"
    assert ran_on[0].startswith("bycall-im-"), f"断开未在 IM loop 线程执行: {ran_on}"
    assert not stop_offloop, "跨线程调用了同步 stop()（会触发 SDK no-current-loop bug）"
    assert ch.is_connected is False
    assert GLOBAL_SLOT_KEY not in mgr._slots
