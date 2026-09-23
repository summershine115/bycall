"""ADR-0012 多 Bot 渠道体系单测（2026-09-22 起 per-Agent 多渠道）。

覆盖多 Bot 平台的核心契约（不真实建连，全程 mock ``aibot.WSClient`` 或假渠道）：
1. 槽位生命周期：``start_if_enabled`` 同时起全局 + 专属连接槽位；清凭证/禁用后
   ``reload_agent`` 停槽位；``stop_agent``/``stop`` 清理。
2. 入站路由：``WecomChannel`` 构造时带 ``agent_id`` → ``InboundMessage.agent_id`` 透传。
3. 会话隔离：同一用户 × 不同实例（含默认）→ 三条独立 Conversation，``agent_id`` 落库。
4. 幂等命名空间：专属 Bot 的 msg_id 按 ``{channel}:agent:{id}`` 隔离；全局 Bot 沿用旧键格式。
5. 实例失效守卫：禁用实例的滞留消息被礼貌拒绝，不回落默认实例。
6. 控制面校验：同一渠道的凭证身份（企微 bot_id）全局唯一（Agent 间 + 与全局默认 Bot）。
7. 多渠道（2026-09-22）：同一实例可同时接入多个渠道（每渠道一条槽位）；存量旧扁平
   im_config 读取兼容；im_config PATCH 按渠道合并/移除。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from sqlmodel import create_engine

from bycall.agent.runner import AgentResult
from bycall.gateway.base import InboundMessage, ReplySender

# ============ 共用 fixture：临时 DB + 模拟 WSClient ============


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含默认 Agent 与 im.* 设置种子）。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

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
    """模拟 aibot WsConnectionManager：吞掉直连发送的帧。"""

    async def send(self, frame) -> None:
        return


class _FakeWSClient:
    """模拟 aibot.WSClient：捕获回调注册，connect 即视为已连接。"""

    def __init__(self, options) -> None:
        self.options = options
        self.handlers: dict[str, list] = {}
        self._connected = False
        self._ws_manager = _FakeWsManager()

    def on(self, event, f=None):
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


def _patch_wsclient(monkeypatch) -> None:
    import aibot

    monkeypatch.setattr(aibot, "WSClient", _FakeWSClient)


def _encrypt_im(bot_id: str, secret: str = "s") -> str:
    """新版按渠道分组格式（2026-09-22 起的写入格式）。"""
    from bycall.governance.secrets import encrypt_secret

    return encrypt_secret(json.dumps({"wecom": {"bot_id": bot_id, "secret": secret}}))


def _encrypt_im_legacy(bot_id: str, secret: str = "s") -> str:
    """旧版扁平格式（ADR-0012 初版）——存量数据的读取兼容用例。"""
    from bycall.governance.secrets import encrypt_secret

    return encrypt_secret(json.dumps({"wecom_bot_id": bot_id, "wecom_secret": secret}))


def _encrypt_im_raw(cfg: dict) -> str:
    """任意结构直接加密（多渠道用例）。"""
    from bycall.governance.secrets import encrypt_secret

    return encrypt_secret(json.dumps(cfg))


def _add_agent(session, name: str, *, enabled: bool = True, im_encrypted: str = "") -> int:
    from bycall.governance.models import Agent

    agent = Agent(
        name=name,
        display_name=name,
        channel="wecom",
        im_config_encrypted=im_encrypted,
        is_enabled=enabled,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent.id


def _text_sender(cap: list | None = None):
    cap = [] if cap is None else cap

    async def send_text(content):
        cap.append(content)

    async def send_stream(content, stream_id, finish, msg_item=None):
        cap.append(content)

    return ReplySender(send_text=send_text, send_stream=send_stream, channel="wecom"), cap


def _answered_result() -> AgentResult:
    return AgentResult(
        answer="ok",
        decision="answered",
        knowledge_scope=["public"],
        retrieved_chunks=1,
        user_name="u",
        role="staff",
        tool_calls=[],
    )


async def _noop_on_message(msg, sender):
    """空消息回调。"""


# ============ 1. 槽位生命周期 ============


class TestMultiSlotLifecycle:
    @pytest.mark.asyncio
    async def test_start_if_enabled_starts_global_and_agent_slots(self, db_session, monkeypatch):
        """im.enable + 全局凭证 + 一个带专属凭证的启用 Agent → 两个槽位同时启动。"""
        from bycall.gateway.manager import GLOBAL_SLOT_KEY, ChannelManager

        setv = self._setv
        setv(db_session, "im.enable", "true")
        setv(db_session, "im.wecom_bot_id", "global-bot")
        setv(db_session, "im.wecom_bot_secret", "gs")
        agent_id = _add_agent(db_session, "bot-agent", im_encrypted=_encrypt_im("agent-bot"))

        _patch_wsclient(monkeypatch)
        mgr = ChannelManager()
        try:
            assert mgr.start_if_enabled() is True
            keys = set(mgr._slots)
            agent_key = f"wecom:agent:{agent_id}"
            assert keys == {GLOBAL_SLOT_KEY, agent_key}

            channels = mgr.status()["channels"]
            by_key = {c["key"]: c for c in channels}
            assert by_key[GLOBAL_SLOT_KEY]["scope"] == "global"
            assert by_key[GLOBAL_SLOT_KEY]["bot_id"] == "global-bot"
            assert by_key[agent_key]["scope"] == "agent"
            assert by_key[agent_key]["agent_id"] == agent_id
            assert by_key[agent_key]["bot_id"] == "agent-bot"
        finally:
            mgr.stop()
        assert not mgr._slots

    @pytest.mark.asyncio
    async def test_disabled_agent_slot_not_started(self, db_session, monkeypatch):
        """禁用实例即使留有凭证也不启动槽位（连接随启停联动）。"""
        from bycall.gateway.manager import GLOBAL_SLOT_KEY, ChannelManager

        self._setv(db_session, "im.enable", "true")
        self._setv(db_session, "im.wecom_bot_id", "global-bot")
        self._setv(db_session, "im.wecom_bot_secret", "gs")
        _add_agent(db_session, "disabled-bot", enabled=False, im_encrypted=_encrypt_im("b2"))

        _patch_wsclient(monkeypatch)
        mgr = ChannelManager()
        try:
            assert mgr.start_if_enabled() is True
            assert set(mgr._slots) == {GLOBAL_SLOT_KEY}
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_reload_agent_stops_slot_when_creds_cleared(self, db_session, monkeypatch):
        """清空 im_config 后 reload_agent → 槽位停止（不再残留僵尸连接）。"""
        from bycall.gateway.manager import ChannelManager

        self._setv(db_session, "im.enable", "true")
        self._setv(db_session, "im.wecom_bot_id", "global-bot")
        self._setv(db_session, "im.wecom_bot_secret", "gs")
        agent_id = _add_agent(db_session, "bot-agent", im_encrypted=_encrypt_im("agent-bot"))

        _patch_wsclient(monkeypatch)
        mgr = ChannelManager()
        try:
            mgr.start_if_enabled()
            # 管理员清除凭证（AgentUpdate im_config={} 的落库效果）
            from bycall.governance.models import Agent

            agent = db_session.get(Agent, agent_id)
            agent.im_config_encrypted = ""
            db_session.add(agent)
            db_session.commit()

            ok, msg = mgr.reload_agent(agent_id)
            assert ok is True
            assert "停止" in msg
            assert f"wecom:agent:{agent_id}" not in mgr._slots
            # 全局槽位不受影响（只动那一个渠道）
            assert "wecom:global" in mgr._slots
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_agent_only_touches_that_slot(self, db_session, monkeypatch):
        """stop_agent 只停指定槽位，其余连接不动。"""
        from bycall.gateway.manager import GLOBAL_SLOT_KEY, ChannelManager

        self._setv(db_session, "im.enable", "true")
        self._setv(db_session, "im.wecom_bot_id", "global-bot")
        self._setv(db_session, "im.wecom_bot_secret", "gs")
        agent_id = _add_agent(db_session, "bot-agent", im_encrypted=_encrypt_im("agent-bot"))

        _patch_wsclient(monkeypatch)
        mgr = ChannelManager()
        try:
            mgr.start_if_enabled()
            assert mgr.stop_agent(agent_id) is True
            assert set(mgr._slots) == {GLOBAL_SLOT_KEY}
            assert mgr.stop_agent(agent_id) is False  # 已停，幂等
        finally:
            mgr.stop()

    @staticmethod
    def _setv(session, key: str, value: str) -> None:
        from bycall.governance.settings_store import set_setting_value

        set_setting_value(session, key, value)
        session.commit()


# ============ 2. 入站路由：连接绑定实例 ============


class TestInboundRouting:
    @pytest.mark.asyncio
    async def test_wecom_message_carries_agent_id(self, monkeypatch):
        """专属 Bot 连接的入站消息带 agent_id；全局连接为 None。"""
        _patch_wsclient(monkeypatch)
        from bycall.gateway.wecom import WecomChannel

        async def run(bot_kwargs: dict) -> int | None:
            got: list[InboundMessage] = []

            async def on_message(msg, sender):
                got.append(msg)

            ch = WecomChannel("b", "s", max_reconnect_attempts=-1, **bot_kwargs)
            await ch.start(on_message)
            frame = {
                "body": {"text": {"content": "hi"}, "from": {"userid": "u1"}},
                "headers": {"req_id": "r1"},
            }
            for h in ch._client.handlers["message.text"]:
                await h(frame)
            ch.stop()
            return got[0].agent_id

        assert await run({"agent_id": 9}) == 9
        assert await run({}) is None


# ============ 3. 会话隔离（实例维度） ============


class TestConversationAgentRouting:
    def test_same_user_distinct_agents_distinct_conversations(self, db_session):
        """同一用户同一渠道：默认实例 / 实例 1 / 实例 2 → 三条独立会话。

        红线 #2 实例维度落地：session_key 带 :agent:{id} 后缀，互不串历史。
        """
        from bycall.agent.session_store import get_or_create_conversation
        from bycall.governance.models import User

        user = User(name="u1", display_name="U1", role="member")
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        c_default = get_or_create_conversation(db_session, user.id, "wecom")
        c_a = get_or_create_conversation(db_session, user.id, "wecom", agent_id=101)
        c_b = get_or_create_conversation(db_session, user.id, "wecom", agent_id=102)

        ids = {c_default.id, c_a.id, c_b.id}
        assert len(ids) == 3
        assert c_default.agent_id is None
        assert c_default.session_key == f"wecom:{user.id}"
        assert c_a.agent_id == 101
        assert c_a.session_key == f"wecom:{user.id}:agent:101"
        assert c_b.session_key == f"wecom:{user.id}:agent:102"

        # 幂等：再次取同一实例会话 → 同一条
        again = get_or_create_conversation(db_session, user.id, "wecom", agent_id=101)
        assert again.id == c_a.id


# ============ 4. 幂等命名空间（Bot 维度） ============


class TestIdempotencyAgentScope:
    def _run_handler(self, msg: InboundMessage, fake_stream) -> None:
        from bycall.agent.im_handler import handle_im_message

        sender, _ = _text_sender()
        from unittest.mock import MagicMock

        limiter = MagicMock()
        limiter.allow.return_value = True
        user = MagicMock(id=7)
        with (
            patch("bycall.agent.im_handler.resolve_user_by_im", return_value=user),
            patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
            patch("bycall.agent.im_handler._run_stream", fake_stream),
        ):
            asyncio.run(handle_im_message(msg, sender))

    def test_same_msgid_two_agents_both_execute(self, db_session):
        """两个专属 Bot 各自收到同号 msg_id → 互不误判重复，各执行一次。"""
        a1 = _add_agent(db_session, "bot-a", im_encrypted=_encrypt_im("ba"))
        a2 = _add_agent(db_session, "bot-b", im_encrypted=_encrypt_im("bb"))
        calls: list[str] = []

        async def fake_stream(user_id, question, channel, im_user_id="", agent_id=None):
            calls.append(f"{agent_id}:{question}")
            yield "", True, _answered_result()

        for agent_id in (a1, a2):
            msg = InboundMessage(
                channel="wecom",
                im_user_id="u1",
                text="公司介绍",
                raw_frame={},
                msg_id="M9",
                agent_id=agent_id,
            )
            self._run_handler(msg, fake_stream)

        assert calls == [f"{a1}:公司介绍", f"{a2}:公司介绍"]

        from bycall.governance.models import IdempotencyRecord

        for agent_id in (a1, a2):
            key = f"wecom:agent:{agent_id}:M9"
            assert db_session.exec(IdempotencyRecord.select_by_key(key)).first() is not None

    def test_global_bot_key_format_backward_compatible(self, db_session):
        """全局默认 Bot 的幂等键保持 ``{channel}:{msg_id}`` 旧格式（存量记录防重放兼容）。"""
        calls: list[str] = []

        async def fake_stream(user_id, question, channel, im_user_id="", agent_id=None):
            calls.append(question)
            yield "", True, _answered_result()

        msg = InboundMessage(
            channel="wecom", im_user_id="u1", text="你好", raw_frame={}, msg_id="M100"
        )
        self._run_handler(msg, fake_stream)

        from bycall.governance.models import IdempotencyRecord

        assert db_session.exec(IdempotencyRecord.select_by_key("wecom:M100")).first() is not None


# ============ 5. 实例失效守卫 ============


class TestAgentDisabledGuard:
    def test_message_for_disabled_agent_rejected(self, db_session):
        """禁用实例的滞留消息 → 礼貌拒绝，绝不进治理链路（不回落默认实例）。"""
        from bycall.agent.im_handler import handle_im_message
        from bycall.governance.models import User

        user = User(name="guard-u", display_name="GU", role="member", im_bindings={"wecom": "gu1"})
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        agent_id = _add_agent(db_session, "dead-bot", enabled=False, im_encrypted=_encrypt_im("bd"))

        cap: list = []
        sender, _ = _text_sender(cap)
        ran: list = []

        async def fake_stream(user_id, question, channel, im_user_id="", agent_id=None):
            ran.append(question)
            yield "", True, _answered_result()

        from unittest.mock import MagicMock

        limiter = MagicMock()
        limiter.allow.return_value = True
        msg = InboundMessage(
            channel="wecom",
            im_user_id="gu1",
            text="你好",
            raw_frame={},
            agent_id=agent_id,
        )
        with (
            patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
            patch("bycall.agent.im_handler._run_stream", fake_stream),
        ):
            asyncio.run(handle_im_message(msg, sender))

        assert not ran  # 未进治理链路
        assert any("停用" in c for c in cap)  # 用户收到了礼貌拒绝

    def test_message_for_enabled_agent_passes_guard(self, db_session):
        """启用实例的消息正常放行进治理链路。"""
        from bycall.agent.im_handler import handle_im_message
        from bycall.governance.models import User

        user = User(name="ok-u", display_name="OU", role="member", im_bindings={"wecom": "ou1"})
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        agent_id = _add_agent(db_session, "live-bot", im_encrypted=_encrypt_im("bl"))

        cap: list = []
        sender, _ = _text_sender(cap)
        seen_agent_ids: list = []

        async def fake_stream(user_id, question, channel, im_user_id="", agent_id=None):
            seen_agent_ids.append(agent_id)
            yield "", True, _answered_result()

        from unittest.mock import MagicMock

        limiter = MagicMock()
        limiter.allow.return_value = True
        msg = InboundMessage(
            channel="wecom", im_user_id="ou1", text="你好", raw_frame={}, agent_id=agent_id
        )
        with (
            patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
            patch("bycall.agent.im_handler._run_stream", fake_stream),
        ):
            asyncio.run(handle_im_message(msg, sender))

        assert seen_agent_ids == [agent_id]


# ============ 6. 控制面校验：wecom_bot_id 全局唯一 ============


class TestAgentImConfigValidation:
    def test_duplicate_bot_id_across_agents_rejected(self, db_session):
        """两个 Agent 用同一 bot_id → 400（企微单连接策略会互踢）。"""
        from fastapi import HTTPException

        from bycall.admin.api import _validate_agent_im_config

        _add_agent(db_session, "owner-bot", im_encrypted=_encrypt_im("dup-bot"))
        with pytest.raises(HTTPException) as exc:
            _validate_agent_im_config(db_session, None, {"wecom_bot_id": "dup-bot"})
        assert exc.value.status_code == 400

    def test_same_agent_update_own_bot_id_allowed(self, db_session):
        """实例更新自己的 bot_id（不变）不视为冲突。"""
        from bycall.admin.api import _validate_agent_im_config

        agent_id = _add_agent(db_session, "self-bot", im_encrypted=_encrypt_im("keep-bot"))
        _validate_agent_im_config(
            db_session, agent_id, {"wecom_bot_id": "keep-bot", "wecom_secret": "s"}
        )  # 不抛

    def test_conflict_with_global_default_bot_rejected(self, db_session):
        """与全局默认 Bot 同号 → 400。"""
        from fastapi import HTTPException

        from bycall.admin.api import _validate_agent_im_config
        from bycall.governance.settings_store import set_setting_value

        set_setting_value(db_session, "im.wecom_bot_id", "global-bot")
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            _validate_agent_im_config(db_session, None, {"wecom_bot_id": "global-bot"})
        assert exc.value.status_code == 400

    def test_distinct_bot_ids_pass(self, db_session):
        """不同 bot_id 正常放行；空 im_config（{}=清空全部）不校验。"""
        from bycall.admin.api import _validate_agent_im_config

        _add_agent(db_session, "a-bot", im_encrypted=_encrypt_im("bot-a"))
        # 新格式与旧扁平格式均可作为载荷
        _validate_agent_im_config(db_session, None, {"wecom": {"bot_id": "bot-b", "secret": "s"}})
        _validate_agent_im_config(db_session, None, {"wecom_bot_id": "bot-b", "wecom_secret": "s"})
        _validate_agent_im_config(db_session, None, {})  # 清空全部，无需校验

    def test_incomplete_credential_rejected(self, db_session):
        """半套凭证（缺 secret）→ 400：配置了也不会建连，写入前拦住。"""
        from fastapi import HTTPException

        from bycall.admin.api import _validate_agent_im_config

        with pytest.raises(HTTPException) as exc:
            _validate_agent_im_config(db_session, None, {"wecom": {"bot_id": "half-bot"}})
        assert exc.value.status_code == 400

    def test_unsupported_channel_type_rejected(self, db_session):
        """未接入连接器的渠道（如钉钉）→ 400：不留「配置了看似生效」的缝隙。"""
        from fastapi import HTTPException

        from bycall.admin.api import _validate_agent_im_config

        with pytest.raises(HTTPException) as exc:
            _validate_agent_im_config(
                db_session, None, {"dingtalk": {"app_id": "f1", "app_secret": "fs"}}
            )
        assert exc.value.status_code == 400
        assert "暂未接入" in exc.value.detail

    def test_feishu_credentials_validated(self, db_session):
        """飞书已接入连接器：完整凭证通过；与其他 Agent 的 app_id 冲突仍被拦。"""
        from bycall.admin.api import _validate_agent_im_config

        # 完整凭证（无冲突）通过——不再 400。
        _validate_agent_im_config(db_session, None, {"feishu": {"app_id": "cli_a", "app_secret": "s"}})

    def test_invalid_payload_shape_rejected(self, db_session):
        """顶层非渠道分组的标量字段 → 400（拼写错误不被静默吞掉）。"""
        from fastapi import HTTPException

        from bycall.admin.api import _validate_agent_im_config

        with pytest.raises(HTTPException) as exc:
            _validate_agent_im_config(db_session, None, {"wecomm": "oops"})
        assert exc.value.status_code == 400


# ============ 7. 多渠道（2026-09-22）：同实例多渠道 + 存量兼容 + PATCH 合并 ============


class _FakeChannelObj:
    """假渠道连接（多渠道槽位/推送测试用，不真实建连）。"""

    def __init__(self, bot_id: str) -> None:
        self._bot_id = bot_id
        self.is_connected = True
        self.sent: list[tuple[str, dict]] = []

    async def start(self, on_message) -> None:
        return

    def stop(self) -> None:
        return

    async def stop_async(self) -> None:
        return

    async def send_proactive(self, chatid: str, body: dict) -> None:
        self.sent.append((chatid, body))


def _wait_slots_ready(slots, timeout: float = 5.0) -> None:
    """等各槽位的事件循环线程就绪（slot.loop 由后台线程异步赋值，有界条件等待）。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(s.loop is not None for s in slots):
            return
        time.sleep(0.02)
    raise AssertionError("IM 槽位事件循环线程未在时限内就绪")


def _patch_fake_channels(monkeypatch, channel_types: set[str]) -> None:
    """把指定渠道类型的连接构造替换为假连接（类级 monkeypatch，保留未列渠道的原实现）。"""
    from bycall.gateway.manager import ChannelManager

    original = ChannelManager._build_channel_for

    def patched(self, channel_type, fields, *, agent_id):
        if channel_type in channel_types:
            return _FakeChannelObj(str(fields.get("bot_id") or fields.get("app_id") or "fake"))
        return original(self, channel_type, fields, agent_id=agent_id)

    monkeypatch.setattr(ChannelManager, "_build_channel_for", patched)


def _add_multi_channel_agent(session, name: str, *, channels: dict) -> int:
    from bycall.governance.models import Agent

    agent = Agent(
        name=name,
        display_name=name,
        channel="wecom",
        im_config_encrypted=_encrypt_im_raw(channels),
        is_enabled=True,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent.id


class TestMultiChannelPerAgent:
    """同一数字员工接入多个不同渠道：每种渠道一条独立槽位，互不干扰。"""

    @pytest.mark.asyncio
    async def test_agent_with_two_channels_gets_two_slots(self, db_session, monkeypatch):
        """wecom + feishu（假连接）→ 两条独立槽位，status() 逐槽位带 channel 字段。"""
        from bycall.gateway.manager import ChannelManager

        self._enable_im(db_session)
        _patch_fake_channels(monkeypatch, {"wecom", "feishu"})
        agent_id = _add_multi_channel_agent(
            db_session,
            "multi-bot",
            channels={
                "wecom": {"bot_id": "multi-w", "secret": "s"},
                "feishu": {"app_id": "multi-f", "app_secret": "fs"},
            },
        )

        mgr = ChannelManager()
        try:
            assert mgr.start_if_enabled() is True
            keys = set(mgr._slots)
            assert keys == {f"wecom:agent:{agent_id}", f"feishu:agent:{agent_id}"}
            by_key = {c["key"]: c for c in mgr.status()["channels"]}
            assert by_key[f"wecom:agent:{agent_id}"]["channel"] == "wecom"
            assert by_key[f"feishu:agent:{agent_id}"]["channel"] == "feishu"
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_legacy_flat_config_still_loads(self, db_session, monkeypatch):
        """存量旧扁平 im_config → 读取归一后照常建槽位（懒升级，无需迁移）。"""
        from bycall.gateway.manager import ChannelManager

        self._enable_im(db_session)
        _patch_fake_channels(monkeypatch, {"wecom"})
        agent_id = _add_agent(db_session, "legacy-bot", im_encrypted=_encrypt_im_legacy("lg-w"))

        mgr = ChannelManager()
        try:
            mgr.start_if_enabled()
            assert set(mgr._slots) == {f"wecom:agent:{agent_id}"}
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_reload_agent_restarts_all_channels(self, db_session, monkeypatch):
        """reload_agent 重建该实例的全部渠道槽位（不是只建第一个）。"""
        from bycall.gateway.manager import ChannelManager

        self._enable_im(db_session)
        _patch_fake_channels(monkeypatch, {"wecom", "feishu"})
        agent_id = _add_multi_channel_agent(
            db_session,
            "reload-bot",
            channels={
                "wecom": {"bot_id": "rb-w", "secret": "s"},
                "feishu": {"app_id": "rb-f", "app_secret": "fs"},
            },
        )

        mgr = ChannelManager()
        try:
            mgr.start_if_enabled()
            ok, msg = mgr.reload_agent(agent_id)
            assert ok is True
            assert "wecom" in msg and "feishu" in msg
            assert set(mgr._slots) == {f"wecom:agent:{agent_id}", f"feishu:agent:{agent_id}"}
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_stop_agent_stops_all_channels(self, db_session, monkeypatch):
        """stop_agent 停掉该实例全部渠道槽位。"""
        from bycall.gateway.manager import ChannelManager

        self._enable_im(db_session)
        _patch_fake_channels(monkeypatch, {"wecom", "feishu"})
        agent_id = _add_multi_channel_agent(
            db_session,
            "stop-bot",
            channels={
                "wecom": {"bot_id": "sb-w", "secret": "s"},
                "feishu": {"app_id": "sb-f", "app_secret": "fs"},
            },
        )

        mgr = ChannelManager()
        try:
            mgr.start_if_enabled()
            assert mgr.stop_agent(agent_id) is True
            assert not mgr._slots
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_send_proactive_routes_by_channel(self, db_session, monkeypatch):
        """多渠道实例按 channel 精确路由推送：feishu 会话的回推走 feishu 连接。"""
        from bycall.gateway.manager import ChannelManager

        self._enable_im(db_session)
        _patch_fake_channels(monkeypatch, {"wecom", "feishu"})
        agent_id = _add_multi_channel_agent(
            db_session,
            "push-bot",
            channels={
                "wecom": {"bot_id": "pb-w", "secret": "s"},
                "feishu": {"app_id": "pb-f", "app_secret": "fs"},
            },
        )

        mgr = ChannelManager()
        try:
            mgr.start_if_enabled()
            wecom_slot = mgr._slots[f"wecom:agent:{agent_id}"]
            feishu_slot = mgr._slots[f"feishu:agent:{agent_id}"]
            _wait_slots_ready([wecom_slot, feishu_slot])  # 事件循环线程就绪后再推（免竞态）
            ok = mgr.send_proactive("u1", {"msgtype": "text"}, agent_id=agent_id, channel="feishu")
            assert ok is True
            assert feishu_slot.channel.sent == [("u1", {"msgtype": "text"})]
            assert wecom_slot.channel.sent == []  # 不会误从企微 Bot 推出
        finally:
            mgr.stop()

    @staticmethod
    def _enable_im(session) -> None:
        from bycall.governance.settings_store import set_setting_value

        set_setting_value(session, "im.enable", "true")
        session.commit()


class TestAgentOutPreview:
    """AgentOut 脱敏预览：按渠道分组；损坏凭证不静默当作未配置。"""

    def test_preview_grouped_by_channel(self, db_session):
        from bycall.admin.api import _agent_to_out
        from bycall.governance.models import Agent

        agent_id = _add_agent(db_session, "preview-bot", im_encrypted=_encrypt_im("pv"))
        agent = db_session.get(Agent, agent_id)
        out = _agent_to_out(agent)
        assert out.im_channels == ["wecom"]
        assert out.im_config_preview == {"wecom": {"bot_id": "***", "secret": "***"}}

    def test_preview_marks_corrupted_credential(self, db_session):
        from bycall.admin.api import _agent_to_out
        from bycall.governance.models import Agent

        agent_id = _add_agent(db_session, "broken-bot", im_encrypted="not-a-fernet-token")
        agent = db_session.get(Agent, agent_id)
        out = _agent_to_out(agent)
        assert out.has_im_config is True
        assert out.im_config_preview == {"_error": "<decrypt failed>"}

class TestImConfigNormalizeAndMerge:
    """im_config 规范化 / PATCH 解析 / 按渠道合并的纯函数契约。"""

    def test_normalize_legacy_flat(self):
        from bycall.gateway.channels import normalize_im_config

        assert normalize_im_config({"wecom_bot_id": "b", "wecom_secret": " s "}) == {
            "wecom": {"bot_id": "b", "secret": "s"}
        }

    def test_normalize_drops_empty_channels(self):
        from bycall.gateway.channels import normalize_im_config

        assert normalize_im_config({"wecom": {"bot_id": "", "secret": ""}, "feishu": {}}) == {}

    def test_parse_patch_keeps_removal_marker(self):
        """{"wecom": {}} 是显式移除信号，不能被丢弃（区别于「未提及」）。"""
        from bycall.gateway.channels import parse_im_config_patch

        assert parse_im_config_patch({"wecom": {}}) == {"wecom": {}}

    def test_parse_patch_invalid_shape_raises(self):
        from bycall.gateway.channels import parse_im_config_patch

        with pytest.raises(ValueError):
            parse_im_config_patch({"wecom": "not-a-dict"})

    def test_merge_replaces_channel_and_preserves_others(self):
        from bycall.gateway.channels import merge_im_config

        current = {"wecom": {"bot_id": "a", "secret": "1"}, "feishu": {"app_id": "f"}}
        merged = merge_im_config(current, {"wecom": {"bot_id": "b", "secret": "2"}})
        assert merged == {"wecom": {"bot_id": "b", "secret": "2"}, "feishu": {"app_id": "f"}}

    def test_merge_empty_clears_all(self):
        from bycall.gateway.channels import merge_im_config

        assert merge_im_config({"wecom": {"bot_id": "a", "secret": "1"}}, {}) == {}

    def test_merge_empty_channel_removes_only_it(self):
        from bycall.gateway.channels import merge_im_config

        merged = merge_im_config(
            {"wecom": {"bot_id": "a", "secret": "1"}, "feishu": {"app_id": "f"}},
            {"wecom": {}},
        )
        assert merged == {"feishu": {"app_id": "f"}}
