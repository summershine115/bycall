"""飞书渠道单测（FeishuChannel + feishu_wire 协议层，ADR-0015）。

不真实建连：WS 与 REST 全部 fake（对齐 test_wecom_m6 的 mock 模式）。协议编解码用
**官方 lark-oapi SDK 1.7.3 生成的 golden bytes** 锚定——wire format 若与官方不一致，
此处会先红。覆盖：

1. wire：帧编解码（golden）/ 未知字段前向兼容 / ClientConfig 解析 / 端点发现分类。
2. 事件映射：union_id 优先、p2p 清空 chat_id（群聊守卫适配）、post/占位文本、system 跳过。
3. 渠道流：事件派发 + ACK（含分片合包、card 不回执）、卡片伪流式与降级、reply 撤回回退、
   主动推送前缀路由、断线重连、stop_async 干净收尾。
4. 接线：全局双渠道构造、飞书专属渠道分派。
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import create_engine

from bycall.config import get_settings

# ============ golden bytes：官方 lark-oapi 1.7.3（pbbp2 proto2）序列化产物 ============

# ping 帧：SeqID=0 LogID=0 service=42 method=CONTROL headers[type]=ping
GOLDEN_PING_HEX = "08001000182a20002a0c0a0474797065120470696e67"
# 事件 DATA 帧：SeqID=7 LogID=9 service=1 method=DATA headers 5 个 + payload
GOLDEN_EVENT_HEX = (
    "08071009180120012a0d0a047479706512056576656e742a130a0a6d6573736167655f69641205"
    "6d2d6162632a0f0a0874726163655f69641203742d312a080a0373756d1201312a080a03736571"
    "12013042517b22736368656d61223a2022322e30222c2022686561646572223a207b226576656e"
    "745f74797065223a2022696d2e6d6573736167652e726563656976655f7631227d2c2022657665"
    "6e74223a207b7d7d"
)
# ACK 帧：echo 头 + biz_rt=12 + Response JSON payload
GOLDEN_ACK_HEX = (
    "08071009180120012a0d0a047479706512056576656e742a130a0a6d6573736167655f69641205"
    "6d2d6162632a0f0a0874726163655f69641203742d312a080a0373756d1201312a080a03736571"
    "1201302a0c0a0662697a5f727412023132422c7b22636f6465223a203230302c20226865616465"
    "7273223a206e756c6c2c202264617461223a206e756c6c7d"
)
# pong 帧携带新配置（生成自官方 SDK，程序化写入防转录错漏）
GOLDEN_PONG_HEX = "08001000180120002a0c0a04747970651204706f6e67425a7b225265636f6e6e656374436f756e74223a202d312c20225265636f6e6e656374496e74657276616c223a203132302c20225265636f6e6e6563744e6f6e6365223a2033302c202250696e67496e74657276616c223a2034357d"


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含 im.* 设置种子，渠道接线用例用）。"""
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


class _FakeWebSocket:
    """模拟 websockets 客户端协议：队列喂帧，close 后 recv 抛 ConnectionClosed。"""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def recv(self) -> bytes:
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.queue.put_nowait(ConnectionError("connection closed by client"))


class _FakeConnectFactory:
    """替代 websockets.connect：记录建连次数，返回可控 fake WS。"""

    def __init__(self) -> None:
        self.sockets: list[_FakeWebSocket] = []
        self.calls = 0
        self.fail_first = 0  # 前 N 次建连抛异常（重连用例）

    async def __call__(self, url: str, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise OSError("connect refused (fake)")
        ws = _FakeWebSocket()
        self.sockets.append(ws)
        return ws


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> None:
    """有界条件等待（ACK 异步任务落盘前的竞态窗口）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("条件等待超时")


def _patch_transport(monkeypatch, *, config=None) -> _FakeConnectFactory:
    """换端点 + WS 建连全部 fake 化（config 可指定重连/心跳节奏）。"""
    from bycall.gateway import feishu as feishu_mod
    from bycall.gateway.feishu_wire import ClientConfig

    factory = _FakeConnectFactory()
    monkeypatch.setattr(
        feishu_mod,
        "fetch_ws_endpoint",
        AsyncMock(
            return_value=(
                "wss://example.feishu.cn/callback?device_id=d1&service_id=7",
                config or ClientConfig(ping_interval=3600, reconnect_interval=0, reconnect_nonce=0),
            )
        ),
    )
    monkeypatch.setattr("websockets.connect", factory)
    return factory


def _event_frame(payload: bytes, *, message_id: str = "om_1", sum_: int = 1, seq: int = 0):
    """用本仓编解码器构造 DATA 事件帧（编解码本身由 golden 用例锚定）。"""
    from bycall.gateway.feishu_wire import (
        FRAME_METHOD_DATA,
        HEADER_MESSAGE_ID,
        HEADER_SEQ,
        HEADER_SUM,
        HEADER_TRACE_ID,
        HEADER_TYPE,
        MSG_TYPE_EVENT,
        Frame,
        encode_frame,
    )

    frame = Frame(
        seq_id=1,
        log_id=2,
        service=7,
        method=FRAME_METHOD_DATA,
        headers=[
            (HEADER_TYPE, MSG_TYPE_EVENT),
            (HEADER_MESSAGE_ID, message_id),
            (HEADER_TRACE_ID, "t-1"),
            (HEADER_SUM, str(sum_)),
            (HEADER_SEQ, str(seq)),
        ],
        payload=payload,
    )
    return encode_frame(frame)


def _receive_event(
    message_type="text",
    content='{"text":"你好"}',
    *,
    chat_type="p2p",
    union_id="on_u1",
    open_id="ou_u1",
    message_id="om_1",
    chat_id="oc_c1",
    event_type="im.message.receive_v1",
) -> bytes:
    """构造 im.message.receive_v1 事件 payload。"""
    return json.dumps(
        {
            "schema": "2.0",
            "header": {"event_id": "e1", "event_type": event_type, "app_id": "cli_x"},
            "event": {
                "sender": {
                    "sender_id": {"union_id": union_id, "open_id": open_id},
                    "sender_type": "user",
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "message_type": message_type,
                    "content": content,
                },
            },
        }
    ).encode("utf-8")


async def _start_channel(monkeypatch, on_message, *, agent_id=None, config=None):
    """起一条 fake 化的 FeishuChannel（返回 (channel, factory)）。"""
    from bycall.gateway.feishu import FeishuChannel

    factory = _patch_transport(monkeypatch, config=config)
    channel = FeishuChannel("cli_test", "secret", agent_id=agent_id)
    task = asyncio.create_task(channel.start(on_message))
    await _wait_until(lambda: factory.sockets and channel.is_connected)
    return channel, factory, task


# ============ 1. wire 编解码（golden 锚定） ============


def test_ping_frame_encodes_to_official_golden():
    """ping 帧序列化与官方 SDK 逐字节一致。"""
    from bycall.gateway.feishu_wire import encode_frame, ping_frame

    assert encode_frame(ping_frame(42)).hex() == GOLDEN_PING_HEX


def test_decode_official_event_golden_and_roundtrip():
    """官方 EVENT golden → 解码字段/头/payload；再编码逐字节一致。"""
    from bycall.gateway.feishu_wire import FRAME_METHOD_DATA, decode_frame, encode_frame

    frame = decode_frame(bytes.fromhex(GOLDEN_EVENT_HEX))
    assert frame.method == FRAME_METHOD_DATA
    assert frame.seq_id == 7 and frame.log_id == 9 and frame.service == 1
    assert frame.header("type") == "event"
    assert frame.header("message_id") == "m-abc"
    assert frame.header("sum") == "1" and frame.header("seq") == "0"
    assert b"im.message.receive_v1" in (frame.payload or b"")
    assert encode_frame(frame).hex() == GOLDEN_EVENT_HEX


def test_ack_frame_matches_official_golden():
    """事件回执帧（echo 头 + biz_rt + Response payload）与官方 golden 一致。"""
    from bycall.gateway.feishu_wire import ack_frame, decode_frame, encode_frame

    frame = decode_frame(bytes.fromhex(GOLDEN_EVENT_HEX))
    ack = ack_frame(frame, biz_rt_ms=12)
    assert ack.header("biz_rt") == "12"
    assert json.loads(ack.payload or b"") == {"code": 200, "headers": None, "data": None}
    assert encode_frame(ack).hex() == GOLDEN_ACK_HEX


def test_pong_config_payload_decodes():
    """pong 帧 payload 是 ClientConfig JSON（服务端运行中调参通道）。"""
    from bycall.gateway.feishu_wire import ClientConfig, decode_frame

    frame = decode_frame(bytes.fromhex(GOLDEN_PONG_HEX))
    assert frame.header("type") == "pong"
    cfg = ClientConfig.from_dict(json.loads(frame.payload or b""))
    assert cfg.ping_interval == 45
    assert cfg.reconnect_interval == 120
    assert cfg.reconnect_count == -1
    assert cfg.reconnect_nonce == 30


def test_decode_skips_unknown_fields():
    """未知字段（官方加字段的前向兼容）：跳过不报错，已知字段照常解出。"""
    from bycall.gateway.feishu_wire import decode_frame

    golden = bytes.fromhex(GOLDEN_EVENT_HEX)
    # 追加未知 varint 字段 (#16) 与未知 length-delimited 字段 (#17)。
    extended = golden + bytes([0x80, 0x01, 0x2A]) + bytes([0x8A, 0x01, 0x03]) + b"xyz"
    frame = decode_frame(extended)
    assert frame.header("message_id") == "m-abc"
    assert b"im.message.receive_v1" in (frame.payload or b"")


def test_client_config_from_dict_tolerant():
    """PascalCase 键解析 + 缺字段容忍（默认 -1/120/30/120）。"""
    from bycall.gateway.feishu_wire import ClientConfig

    default = ClientConfig.from_dict(None)
    assert (default.reconnect_count, default.reconnect_interval) == (-1, 120)
    assert (default.reconnect_nonce, default.ping_interval) == (30, 120)
    cfg = ClientConfig.from_dict({"PingInterval": 30, "ReconnectCount": 5})
    assert cfg.ping_interval == 30 and cfg.reconnect_count == 5


@pytest.mark.asyncio
async def test_fetch_ws_endpoint_classification():
    """端点发现：成功/业务错误（致命）/system busy 与 HTTP 异常（瞬态）/缺 URL。"""
    from bycall.gateway.feishu_wire import (
        FeishuAuthError,
        FeishuEndpointError,
        fetch_ws_endpoint,
    )

    def _client(payload, status=200):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload))
        )

    ok = _client(
        {
            "code": 0,
            "data": {
                "URL": "wss://x?device_id=d&service_id=3",
                "ClientConfig": {"PingInterval": 60},
            },
        }
    )
    async with ok as client:
        url, cfg = await fetch_ws_endpoint(
            client, domain="https://open.feishu.cn", app_id="a", app_secret="s"
        )
    assert url.startswith("wss://") and cfg.ping_interval == 60

    async with _client({"code": 10003, "msg": "app secret mismatch"}) as client:
        with pytest.raises(FeishuAuthError):
            await fetch_ws_endpoint(
                client, domain="https://open.feishu.cn", app_id="a", app_secret="s"
            )

    async with _client({"code": 1, "msg": "system busy"}) as client:
        with pytest.raises(FeishuEndpointError):
            await fetch_ws_endpoint(
                client, domain="https://open.feishu.cn", app_id="a", app_secret="s"
            )

    async with _client({}, status=502) as client:
        with pytest.raises(FeishuEndpointError):
            await fetch_ws_endpoint(
                client, domain="https://open.feishu.cn", app_id="a", app_secret="s"
            )

    async with _client({"code": 0, "data": {}}) as client:
        with pytest.raises(FeishuAuthError):
            await fetch_ws_endpoint(
                client, domain="https://open.feishu.cn", app_id="a", app_secret="s"
            )


def test_parse_conn_params():
    """从 WSS URL query 抽 device_id / service_id（ping 帧依赖）。"""
    from bycall.gateway.feishu_wire import parse_conn_params

    device, service = parse_conn_params("wss://x.cn/callback?device_id=dev9&service_id=42")
    assert (device, service) == ("dev9", 42)


# ============ 2. 事件映射（身份/chat_id/文本抽取） ============


def test_p2p_message_chat_id_emptied_group_kept():
    """关键适配：p2p 也有 oc_ chat_id 但必须清空（群聊守卫语义）；群聊保留。"""
    from bycall.gateway.feishu import _map_event_to_message

    p2p = _map_event_to_message(json.loads(_receive_event(chat_type="p2p")), agent_id=None)
    assert p2p is not None and p2p[0].chat_id == ""
    # 回复上下文仍携带真实 chat_id（create 回退要用）。
    assert p2p[1].chat_id == "oc_c1"

    group = _map_event_to_message(
        json.loads(_receive_event(chat_type="group", chat_id="oc_g9")), agent_id=None
    )
    assert group is not None and group[0].chat_id == "oc_g9"


def test_union_id_preferred_over_open_id():
    """union_id 优先（ADR-0015：open_id 是 app 维度，多 app 下同人不稳定）。"""
    from bycall.gateway.feishu import _map_event_to_message

    both = _map_event_to_message(json.loads(_receive_event()), agent_id=None)
    assert both[0].im_user_id == "on_u1"
    only_open = _map_event_to_message(json.loads(_receive_event(union_id="")), agent_id=None)
    assert only_open[0].im_user_id == "ou_u1"


def test_text_extraction_and_agent_id_passthrough():
    """文本抽取 + agent_id 透传（per-Agent 专属 Bot 路由）。"""
    from bycall.gateway.feishu import _map_event_to_message

    msg, _ = _map_event_to_message(json.loads(_receive_event()), agent_id=5)
    assert msg.text == "你好"
    assert msg.channel == "feishu" and msg.agent_id == 5 and msg.msg_id == "om_1"

    mention = _map_event_to_message(
        json.loads(_receive_event(content='{"text":"@_user_1 查一下库存"}')), agent_id=None
    )
    assert mention[0].text == "查一下库存"


def test_post_and_non_text_and_system():
    """post 富文本拼接；非文本占位；system 消息不投递。"""
    from bycall.gateway.feishu import _extract_text, _map_event_to_message

    post = json.dumps(
        {
            "title": "周报",
            "content": [
                [{"tag": "text", "text": "销售"}, {"tag": "a", "text": "详情", "href": "u"}],
            ],
        }
    )
    msg, _ = _map_event_to_message(
        json.loads(_receive_event(message_type="post", content=post)), agent_id=None
    )
    assert msg.text == "周报\n销售\n详情" and msg.message_type == "text"

    img, _ = _map_event_to_message(
        json.loads(_receive_event(message_type="image", content="{}")), agent_id=None
    )
    assert "图片" in img.text and img.message_type == "image"

    assert _extract_text("system", "{}") is None
    assert (
        _map_event_to_message(json.loads(_receive_event(message_type="system")), agent_id=None)
        is None
    )


# ============ 3. 渠道流（派发/ACK/流式/降级/推送/重连/停止） ============


@pytest.mark.asyncio
async def test_event_dispatch_and_ack(monkeypatch):
    """事件帧 → InboundMessage 投递 + ACK 回写（含 biz_rt 头与 code=200）。"""
    received = []

    async def on_message(msg, sender):
        received.append((msg, sender))

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    try:
        ws = factory.sockets[0]
        ws.queue.put_nowait(_event_frame(_receive_event()))
        await _wait_until(lambda: received and ws.sent)
        msg, _sender = received[0]
        assert msg.text == "你好" and msg.channel == "feishu" and msg.msg_id == "om_1"

        from bycall.gateway.feishu_wire import HEADER_BIZ_RT, decode_frame

        ack = decode_frame(ws.sent[0])
        assert ack.header(HEADER_BIZ_RT) is not None
        assert json.loads(ack.payload or b"")["code"] == 200
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_unsubscribed_event_acked_without_dispatch(monkeypatch):
    """非订阅事件（如联系人变更）：不投递数据面，但仍回 ACK 防重推。"""
    received = []

    async def on_message(msg, sender):
        received.append(msg)

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    try:
        ws = factory.sockets[0]
        ws.queue.put_nowait(_event_frame(_receive_event(event_type="contact.user.updated_v1")))
        await _wait_until(lambda: ws.sent)
        assert received == []
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_fragmented_event_reassembled_single_dispatch(monkeypatch):
    """多分片事件：两片到齐才派发一次；未齐分片不回执（对齐官方）。"""
    received = []

    async def on_message(msg, sender):
        received.append(msg)

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    try:
        ws = factory.sockets[0]
        payload = _receive_event(message_id="om_frag", content='{"text":"分片消息"}')
        half = len(payload) // 2
        ws.queue.put_nowait(_event_frame(payload[:half], message_id="om_frag", sum_=2, seq=0))
        await asyncio.sleep(0.05)
        assert ws.sent == [] and received == []  # 未齐：不回执不派发
        ws.queue.put_nowait(_event_frame(payload[half:], message_id="om_frag", sum_=2, seq=1))
        await _wait_until(lambda: received and ws.sent)
        assert received[0].text == "分片消息"
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_stream_reply_card_flow(monkeypatch):
    """卡片伪流式：首帧建卡 → 后续 PUT 更新 → finish 帧附 citation markdown。"""
    received = []

    async def on_message(msg, sender):
        received.append((msg, sender))

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    creates, updates, replies = [], [], []

    async def fake_create_stream_card(ctx, content):
        creates.append(content)
        return "om_card_1"

    async def fake_update_card(message_id, content_md):
        updates.append((message_id, content_md))

    async def fake_reply(message_id, content):
        replies.append((message_id, content))

    channel._create_stream_card = fake_create_stream_card
    channel._update_card = fake_update_card
    channel._reply_message = fake_reply
    try:
        ws = factory.sockets[0]
        ws.queue.put_nowait(_event_frame(_receive_event()))
        await _wait_until(lambda: received)
        _msg, sender = received[0]

        await sender.send_stream("正在思考…", "s1", False)
        await sender.send_stream("上季度", "s1", False)
        await sender.send_stream(
            "上季度销售额 100 万",
            "s1",
            True,
            msg_item=[{"title": "销售报表", "url": "https://k/1", "picurl": ""}],
        )
        assert creates == ["正在思考…"]
        assert [c for _, c in updates] == [
            "上季度",
            "上季度销售额 100 万\n\n**参考来源**\n- [销售报表](https://k/1)",
        ]
        assert replies == []  # 卡片路径成功，无需文本兜底
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_stream_card_failure_falls_back_to_text(monkeypatch):
    """建卡失败 → finish 帧降级文本（reply），内容不丢。"""
    received = []

    async def on_message(msg, sender):
        received.append((msg, sender))

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    replies, creates = [], []

    async def fake_create_stream_card(ctx, content):
        raise Exception("card api down")

    async def fake_reply(message_id, content):
        replies.append((message_id, content))

    channel._create_stream_card = fake_create_stream_card
    channel._reply_message = fake_reply
    try:
        factory.sockets[0].queue.put_nowait(_event_frame(_receive_event()))
        await _wait_until(lambda: received)
        _msg, sender = received[0]
        await sender.send_stream("部分", "s1", False)
        await sender.send_stream("全部内容", "s1", True)
        assert creates == []
        assert replies == [("om_1", "全部内容")]
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_send_text_reply_gone_falls_back_to_create(monkeypatch):
    """回复目标被撤回（230011）→ 回退 create 到会话。"""
    received = []

    async def on_message(msg, sender):
        received.append((msg, sender))

    channel, factory, task = await _start_channel(monkeypatch, on_message)

    from bycall.gateway.feishu import FeishuApiError

    async def reply_gone(message_id, content):
        raise FeishuApiError(230011, "message withdrawn")

    created = []

    async def fake_create(receive_id, content, *, receive_id_type=None):
        created.append((receive_id, content, receive_id_type))

    channel._reply_message = reply_gone
    channel._create_message = fake_create
    try:
        factory.sockets[0].queue.put_nowait(_event_frame(_receive_event()))
        await _wait_until(lambda: received)
        _msg, sender = received[0]
        await sender.send_text("你好，答复")
        assert created == [("oc_c1", "你好，答复", None)]
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_send_proactive_routes_by_id_prefix(monkeypatch):
    """主动推送：wecom 风格 body 翻译为文本消息，receive_id_type 按 ID 前缀路由。"""
    channel, _factory, task = await _start_channel(monkeypatch, lambda m, s: None)
    sent = []

    async def fake_create(receive_id, content, *, receive_id_type=None):
        sent.append((receive_id, content, receive_id_type))

    channel._create_message = fake_create
    try:
        await channel.send_proactive(
            "oc_c1", {"msgtype": "markdown", "markdown": {"content": "**审批通过**"}}
        )
        await channel.send_proactive("ou_9", {"msgtype": "text", "text": {"content": "hi"}})
        await channel.send_proactive("on_9", {"msgtype": "text", "text": {"content": "yo"}})
        assert sent == [
            ("oc_c1", "**审批通过**", None),
            ("ou_9", "hi", None),
            ("on_9", "yo", None),
        ]
        # 前缀路由（_create_message 收到的最终 receive_id_type 由内部决定）
        from bycall.gateway.feishu import _receive_id_type

        assert _receive_id_type("oc_c1") == "chat_id"
        assert _receive_id_type("ou_9") == "open_id"
        assert _receive_id_type("on_9") == "union_id"
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_reconnect_after_connection_drop(monkeypatch):
    """连接断开 → 按服务端配置重连（interval=0 立即重试），新连接继续收事件。"""
    received = []

    async def on_message(msg, sender):
        received.append(msg)

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    try:
        ws1 = factory.sockets[0]
        ws1.queue.put_nowait(ConnectionError("network flap"))
        await _wait_until(lambda: len(factory.sockets) >= 2)
        ws2 = factory.sockets[1]
        ws2.queue.put_nowait(_event_frame(_receive_event(message_id="om_2")))
        await _wait_until(lambda: received)
        assert received[0].msg_id == "om_2"
        assert channel.is_connected
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_stop_async_closes_cleanly(monkeypatch):
    """stop_async：真关 socket、连接态复位、start() 收尾退出。"""
    channel, factory, task = await _start_channel(monkeypatch, lambda m, s: None)
    ws = factory.sockets[0]
    assert channel.is_connected
    await channel.stop_async()
    assert ws.closed
    assert not channel.is_connected
    await asyncio.wait_for(task, timeout=5)  # start() 已收尾（不悬挂）


@pytest.mark.asyncio
async def test_p2p_group_guard_via_im_handler_contract(monkeypatch):
    """端到端契约：p2p 消息的 chat_id 为空 → 不会被数据面群聊守卫误拒（守卫输入形态）。"""
    received = []

    async def on_message(msg, sender):
        received.append(msg)

    channel, factory, task = await _start_channel(monkeypatch, on_message)
    try:
        factory.sockets[0].queue.put_nowait(_event_frame(_receive_event(chat_type="p2p")))
        await _wait_until(lambda: received)
        # im_handler 的守卫是 `if msg.chat_id: 拒绝` —— 此处必须为空。
        assert received[0].chat_id == ""
    finally:
        await channel.stop_async()
        await asyncio.wait_for(task, timeout=5)


# ============ 4. 接线（全局双渠道 + 专属分派 + 注册表） ============


def test_channel_registries_include_feishu():
    """channels.py 注册表：feishu 已接入，身份字段 app_id，必填 app_id/app_secret。"""
    from bycall.gateway.channels import (
        CREDENTIAL_ID_FIELDS,
        REQUIRED_FIELDS,
        SUPPORTED_CHANNEL_TYPES,
        global_credential_setting_key,
    )

    assert "feishu" in SUPPORTED_CHANNEL_TYPES
    assert CREDENTIAL_ID_FIELDS["feishu"] == "app_id"
    assert REQUIRED_FIELDS["feishu"] == ("app_id", "app_secret")
    assert global_credential_setting_key("feishu") == "im.feishu_app_id"


def test_build_global_channels_wecom_and_feishu(db_session):
    """全局双渠道：两套凭证齐 → 两条全局槽位渠道；只配一套 → 只建一条。"""
    from bycall.gateway.feishu import FeishuChannel
    from bycall.gateway.manager import ChannelManager
    from bycall.gateway.wecom import WecomChannel
    from bycall.governance.settings_store import set_setting_value

    set_setting_value(db_session, "im.enable", "true")
    set_setting_value(db_session, "im.wecom_bot_id", "w1")
    set_setting_value(db_session, "im.wecom_bot_secret", "ws")
    db_session.commit()
    mgr = ChannelManager()
    channels, errors = mgr._build_global_channels()
    assert [t for t, _ in channels] == ["wecom"]
    assert any("feishu" in e for e in errors)

    set_setting_value(db_session, "im.feishu_app_id", "cli_a")
    set_setting_value(db_session, "im.feishu_app_secret", "fs")
    db_session.commit()
    channels, errors = mgr._build_global_channels()
    types = dict(channels)
    assert set(types) == {"wecom", "feishu"} and not errors
    assert isinstance(types["wecom"], WecomChannel)
    assert isinstance(types["feishu"], FeishuChannel)


def test_build_agent_feishu_channel_dispatch(db_session):
    """专属渠道分派：feishu 凭证 → FeishuChannel 且携带 agent_id。"""
    from bycall.gateway.manager import ChannelManager

    mgr = ChannelManager()
    channel = mgr._build_channel_for("feishu", {"app_id": "cli_b", "app_secret": "s"}, agent_id=3)
    assert channel is not None
    assert channel._app_id == "cli_b" and channel._agent_id == 3
    assert mgr._build_channel_for("feishu", {"app_id": "cli_b"}, agent_id=3) is None
    assert mgr._build_channel_for("dingtalk", {"x": "1"}, agent_id=3) is None
