"""网关层单测：身份映射 + InboundMessage/ReplySender 构造 + 企微 frame 解析。

不测真实 SDK 连接（需要企微凭证），只测：
1. resolve_user_by_im：命中 / 未绑定 / 禁用用户。
2. InboundMessage / ReplySender 数据类构造。
3. wecom._parse_text_frame：frame → (im_user_id, text, chat_id) 的解析正确性
   （锁定 body.from.userid 这条真实路径，以及 headers 兜底）。
4. wecom 代理绕过：``_should_bypass_proxy`` 判定 + ``_ensure_wecom_bypasses_proxy`` 对企微
   host 注入 ``proxy=None``（修复 SOCKS 代理导致 IM 无法聊天的问题；不真实建连）。
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.gateway.identity import resolve_user_by_im
from bycall.governance.models import User


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含 admin/member/默认提示词）。

    每个用例独立文件库，互不污染。
    """
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


def _make_user(session, name: str, im_bindings: dict | None = None, is_active: bool = True) -> User:
    """插入一个带 im_bindings 的 User 并 flush 拿到 id。"""
    user = User(
        name=name,
        display_name=name,
        role="member",
        im_bindings=im_bindings or {},
        is_active=is_active,
    )
    session.add(user)
    session.flush()
    return user


# ============ resolve_user_by_im ============


def test_resolve_user_by_im_hit(db_session):
    """(channel, im_user_id) 命中 im_bindings → 返回该 active User。"""
    user = _make_user(db_session, "alice", im_bindings={"wecom": "u123"})
    found = resolve_user_by_im(db_session, "wecom", "u123")

    assert found is not None
    assert found.id == user.id
    assert found.name == "alice"


def test_resolve_user_by_im_not_bound_returns_none(db_session):
    """无任何用户绑定该 im_user_id → None（未绑定，邀请制拒绝）。"""
    _make_user(db_session, "alice", im_bindings={"wecom": "u123"})

    # 不存在的 im_user_id
    assert resolve_user_by_im(db_session, "wecom", "nobody") is None
    # 不存在的 channel
    assert resolve_user_by_im(db_session, "feishu", "u123") is None
    # 无 im_bindings 的用户
    _make_user(db_session, "bob", im_bindings={})
    assert resolve_user_by_im(db_session, "wecom", "u999") is None


def test_resolve_user_by_im_inactive_user_returns_none(db_session):
    """禁用用户（is_active=False）不在 select_by_im 候选集 → None。"""
    _make_user(
        db_session,
        "eve",
        im_bindings={"wecom": "u456"},
        is_active=False,
    )
    assert resolve_user_by_im(db_session, "wecom", "u456") is None


def test_resolve_user_by_im_empty_id_returns_none(db_session):
    """空 im_user_id（frame 缺 from.userid）→ None，避免误匹配空绑定。"""
    _make_user(db_session, "alice", im_bindings={"wecom": "u123"})
    assert resolve_user_by_im(db_session, "wecom", "") is None


def test_resolve_user_by_im_multiple_channels(db_session):
    """同一员工在多渠道绑定，按各自 channel+id 精确命中。"""
    _make_user(
        db_session,
        "carol",
        im_bindings={"wecom": "w_carol", "feishu": "f_carol"},
    )
    assert resolve_user_by_im(db_session, "wecom", "w_carol") is not None
    assert resolve_user_by_im(db_session, "feishu", "f_carol") is not None
    # 串渠道：拿企微 id 查飞书 → 不命中
    assert resolve_user_by_im(db_session, "feishu", "w_carol") is None


def test_resolve_user_by_im_distinct_users_same_id_different_channel(db_session):
    """两个用户在不同渠道绑定同一个 im_user_id，各自渠道正确命中。"""
    u1 = _make_user(db_session, "d1", im_bindings={"wecom": "shared"})
    u2 = _make_user(db_session, "d2", im_bindings={"feishu": "shared"})
    assert resolve_user_by_im(db_session, "wecom", "shared").id == u1.id
    assert resolve_user_by_im(db_session, "feishu", "shared").id == u2.id


# ============ InboundMessage / ReplySender 数据类 ============


def test_inbound_message_construction():
    """InboundMessage 字段构造正常，默认值正确。"""
    frame = {"headers": {"req_id": "r1"}, "body": {"text": {"content": "hi"}}}
    msg = InboundMessage(
        channel="wecom",
        im_user_id="u123",
        text="hi",
        raw_frame=frame,
    )

    assert msg.channel == "wecom"
    assert msg.im_user_id == "u123"
    assert msg.text == "hi"
    assert msg.raw_frame is frame
    # 默认值
    assert msg.chat_id == ""
    assert msg.message_type == "text"


def test_reply_sender_construction():
    """ReplySender 接受 async callable，可选 send_stream。"""
    calls = []

    async def send_text(content: str) -> None:
        calls.append(("text", content))

    async def send_stream(content: str, stream_id: str, finish: bool) -> None:
        calls.append(("stream", content, stream_id, finish))

    sender = ReplySender(send_text=send_text, send_stream=send_stream, channel="wecom")

    assert sender.channel == "wecom"
    assert sender.send_stream is not None
    # callable 可调用即可（真正 await 在集成测试里）
    assert callable(sender.send_text)


def test_reply_sender_minimal_without_stream():
    """无 send_stream 时该字段为 None（渠道不支持流式的合法降级）。"""

    async def send_text(content: str) -> None:
        return None

    sender = ReplySender(send_text=send_text)
    assert sender.send_stream is None


# ============ wecom frame 解析 ============


def test_wecom_parse_text_frame_userid_from_body():
    """企微 frame 的发送者 userid 在 body.from.userid（官方文档 BaseMessage.from.userid）。

    锁定这条真实路径，任务说明里的 headers.from_user.user_id 是不准确的。
    """
    from bycall.gateway.wecom import _parse_text_frame

    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "msgtype": "text",
            "text": {"content": "你好"},
            "from": {"userid": "u_from_body"},
            "chatid": "group_xyz",
        },
    }
    im_user_id, text, chat_id = _parse_text_frame(frame)

    assert im_user_id == "u_from_body"
    assert text == "你好"
    assert chat_id == "group_xyz"


def test_wecom_parse_text_frame_headers_fallback():
    """部分实现/转发把身份放 headers.from_user.user_id，作为兜底也支持。"""
    from bycall.gateway.wecom import _parse_text_frame

    frame = {
        "headers": {
            "req_id": "r1",
            "from_user": {"user_id": "u_from_headers"},
        },
        "body": {"msgtype": "text", "text": {"content": "hello"}},
    }
    im_user_id, text, chat_id = _parse_text_frame(frame)

    assert im_user_id == "u_from_headers"
    assert text == "hello"
    assert chat_id == ""


def test_wecom_parse_text_frame_single_chat_no_chatid():
    """单聊消息无 chatid（chattype=single），解析得到空 chat_id。"""
    from bycall.gateway.wecom import _parse_text_frame

    frame = {
        "headers": {"req_id": "r1"},
        "body": {
            "msgtype": "text",
            "text": {"content": "私聊"},
            "from": {"userid": "u1"},
            "chattype": "single",
        },
    }
    im_user_id, text, chat_id = _parse_text_frame(frame)

    assert im_user_id == "u1"
    assert text == "私聊"
    assert chat_id == ""


# ============ wecom WebSocket 代理绕过 ============


def test_should_bypass_proxy_wecom_hosts():
    """企微相关 host（含子域 / 自定义端口 / path）一律判为绕过代理。"""
    from bycall.gateway.wecom import _should_bypass_proxy

    assert _should_bypass_proxy("wss://openws.work.weixin.qq.com")
    assert _should_bypass_proxy("wss://openws.work.weixin.qq.com:443/path")
    assert _should_bypass_proxy("wss://custom.work.weixin.qq.com")


def test_should_bypass_proxy_other_hosts():
    """非企微 host（外网 LLM / 飞书等）不绕过，仍走配置的代理。"""
    from bycall.gateway.wecom import _should_bypass_proxy

    assert not _should_bypass_proxy("wss://api.openai.com")
    assert not _should_bypass_proxy("wss://open.feishu.cn")
    # 无 host 的畸形输入也不误判为绕过（避免把空绑定/异常 URI 当企微直连）
    assert not _should_bypass_proxy("")
    assert not _should_bypass_proxy("not-a-url")


def test_wecom_ws_connect_bypasses_proxy_for_wecom_host():
    """应用补丁后，企微 host 的连接 proxy=None（直连，不再触发 SOCKS/python-socks 报错）。"""
    import websockets

    from bycall.gateway.wecom import _ensure_wecom_bypasses_proxy

    orig = websockets.connect
    try:
        websockets.connect = orig  # 复位到原始类，确保测到「首次应用」
        _ensure_wecom_bypasses_proxy()
        # 构造 Connect 对象不建连（网络 IO 在 await 时才发生），仅验证 proxy 注入
        conn = websockets.connect("wss://openws.work.weixin.qq.com", ssl=True)
        assert conn.proxy is None
    finally:
        websockets.connect = orig


def test_wecom_ws_connect_keeps_proxy_for_other_host():
    """非企微 host 不受补丁影响，proxy 保持默认 True（仍可走代理）。"""
    import websockets

    from bycall.gateway.wecom import _ensure_wecom_bypasses_proxy

    orig = websockets.connect
    try:
        websockets.connect = orig
        _ensure_wecom_bypasses_proxy()
        conn = websockets.connect("wss://api.openai.com", ssl=True)
        assert conn.proxy is True
    finally:
        websockets.connect = orig


def test_ensure_wecom_bypasses_proxy_is_idempotent():
    """重复调用不会二次包裹（幂等 guard 生效）。"""
    import websockets

    from bycall.gateway.wecom import _ensure_wecom_bypasses_proxy

    orig = websockets.connect
    try:
        websockets.connect = orig
        _ensure_wecom_bypasses_proxy()
        first = websockets.connect
        _ensure_wecom_bypasses_proxy()  # 再次调用应 no-op
        assert websockets.connect is first
    finally:
        websockets.connect = orig
