"""IM 消息处理器集成测试 —— 验证企微渠道接入治理链路。

用 mock ReplySender 捕获回复，mock LLM 跑通治理链路。
验证：未绑定用户拒绝、无权知识拒绝、有权用户得到答案、流式回复顺序。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.governance.models import Role, RoleConfig, User
from bycall.governance.security import hash_password


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_file}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "8")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False}))
    init_db()
    return tmp_path


def _make_mock_llm(reply="基于知识库的回答。"):
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk

    m = MagicMock(spec=BaseChatModel)
    m.invoke.return_value = AIMessage(content=reply)
    m.bind_tools.return_value = m

    # astream: 异步生成器，模拟 token-by-token 流式输出。
    async def _astream(messages, **kwargs):
        for char in reply:
            yield AIMessageChunk(content=char)

    m.astream = _astream
    return m


def _make_sender(capture: list):
    """构造一个 mock ReplySender，把所有回复记进 capture 列表。"""

    async def send_text(content):
        capture.append(("text", content))

    async def send_stream(content, stream_id, finish):
        capture.append(("stream", content, stream_id, finish))

    return ReplySender(send_text=send_text, send_stream=send_stream, channel="wecom")


def _seed_user(name, role, scope_tags):
    """建角色 + 用户（带 im_bindings 便于身份映射命中）。"""
    with session_scope() as s:
        if role != "member":
            s.add(
                Role(
                    name=role,
                    description="",
                    config=RoleConfig(knowledge_scope_tags=scope_tags, level=1),
                )
            )
        s.add(
            User(
                name=name,
                display_name=name,
                role=role,
                password_hash=hash_password("x"),
                is_active=True,
                im_bindings={"wecom": f"im_{name}"},
            )
        )


def _patch_chat_model(monkeypatch, mock_llm):
    """patch resolver.get_chat_model 返回 (假 Model, mock ChatModel)。
    im_handler._run_stream 内部 import get_chat_model，所以 patch 源模块。
    """
    from bycall.llm import resolver

    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(resolver, "get_chat_model", lambda session: (fake_model, mock_llm))
    # im_handler 在函数内 import，patch 后再调时已是新函数
    return mock_llm


def test_unbound_user_rejected(env, monkeypatch):
    """未绑定（未邀请）的 IM 用户 → 友好拒绝。"""
    from bycall.agent import im_handler

    _patch_chat_model(monkeypatch, _make_mock_llm())
    capture = []
    sender = _make_sender(capture)
    msg = InboundMessage(channel="wecom", im_user_id="im_stranger", text="你好")

    asyncio.run(im_handler.handle_im_message(msg, sender))

    assert len(capture) == 1
    assert "未被授权" in capture[0][1] or "联系管理员" in capture[0][1]


def test_user_with_no_scope_denied(env, monkeypatch):
    """绑定但无知识域权限的用户 → denied_no_access 友好提示。"""
    _seed_user("bob", "intern", [])
    from bycall.agent import im_handler

    _patch_chat_model(monkeypatch, _make_mock_llm())
    capture = []
    sender = _make_sender(capture)
    msg = InboundMessage(channel="wecom", im_user_id="im_bob", text="机密信息")

    asyncio.run(im_handler.handle_im_message(msg, sender))

    # 应有流式"思考中" + 最终拒绝提示
    final = [c for c in capture if c[0] == "stream" and c[3] is True]
    assert len(final) == 1
    assert "没有访问相关知识的权限" in final[0][1]


def test_authorized_user_gets_answer(env, monkeypatch):
    """有权用户 → 走完链路得到 LLM 回答；finish 帧携带完整答案（replace 语义）。"""
    _seed_user("erin", "staff", ["public"])
    from bycall.agent import im_handler

    # 关闭节流，确保每个 chunk 都发一帧（断言中间帧为累积全量）。
    monkeypatch.setattr(im_handler, "STREAM_FLUSH_INTERVAL", 0.0)
    reply = "这是基于知识的答案。"
    mock_llm = _make_mock_llm(reply)
    _patch_chat_model(monkeypatch, mock_llm)
    capture = []
    sender = _make_sender(capture)
    msg = InboundMessage(channel="wecom", im_user_id="im_erin", text="公司介绍")

    asyncio.run(im_handler.handle_im_message(msg, sender))

    streams = [c for c in capture if c[0] == "stream"]
    assert streams, "应有流式帧"
    # finish 帧携带完整 final_answer（replace 语义覆盖全文），不再依赖中间帧聚合。
    finish_frames = [c for c in streams if c[3] is True]
    assert len(finish_frames) == 1
    assert finish_frames[0][1] == reply


def test_thinking_placeholder(env, monkeypatch):
    """流式开始立即发"正在思考…"占位，覆盖首 token 前的空白期。"""
    _seed_user("tia", "staff", ["public"])
    from bycall.agent import im_handler

    monkeypatch.setattr(im_handler, "STREAM_FLUSH_INTERVAL", 0.0)
    _patch_chat_model(monkeypatch, _make_mock_llm("答案。"))
    capture = []
    sender = _make_sender(capture)
    msg = InboundMessage(channel="wecom", im_user_id="im_tia", text="你好")

    asyncio.run(im_handler.handle_im_message(msg, sender))

    streams = [c for c in capture if c[0] == "stream"]
    assert streams, "应有流式帧"
    # 第一帧是"正在思考…"占位（finish=False），在真实 chunk 之前发出。
    assert streams[0][1] == "正在思考…"
    assert streams[0][3] is False


def test_full_replacement_frames(env, monkeypatch):
    """中间帧是累积全量递增（replace 语义），不是单字符增量。"""
    _seed_user("rep", "staff", ["public"])
    from bycall.agent import im_handler

    monkeypatch.setattr(im_handler, "STREAM_FLUSH_INTERVAL", 0.0)
    reply = "abcd"
    mock_llm = _make_mock_llm(reply)
    _patch_chat_model(monkeypatch, mock_llm)
    capture = []
    sender = _make_sender(capture)
    msg = InboundMessage(channel="wecom", im_user_id="im_rep", text="你好")

    asyncio.run(im_handler.handle_im_message(msg, sender))

    streams = [c for c in capture if c[0] == "stream"]
    # 中间帧 = 排除"正在思考…"占位帧与 finish 帧。
    middle = [c for c in streams if c[3] is False and c[1] != "正在思考…"]
    # 第 N 帧含前 N 个字符（累积全量、replace 语义），不是每帧单字符增量。
    assert [c[1] for c in middle] == ["a", "ab", "abc", "abcd"]


def test_empty_message_ignored(env, monkeypatch):
    """空消息 → 不处理（不回复、不报错）。"""
    from bycall.agent import im_handler

    _patch_chat_model(monkeypatch, _make_mock_llm())
    capture = []
    sender = _make_sender(capture)
    msg = InboundMessage(channel="wecom", im_user_id="im_x", text="   ")

    asyncio.run(im_handler.handle_im_message(msg, sender))
    assert capture == []
