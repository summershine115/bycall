"""IM 处理器会话稳健性集成测试——会话租约 / 失败回填 / busy 提示。

覆盖 M3 会话稳健性地基的端到端行为：走**真实** ``_run_stream`` + **真实**租约/回填
（跑在真实 SQLite 上），仅桩 LLM 解析、限流、身份映射与 ``run_agent_stream``。

验收点：
- 会话被占用 → ``_busy_result``（提示稍后重试），不执行治理链路。
- turn 结束（正常 / 异常）租约都释放。
- 执行失败 → user 消息已持久化 + 一条 system 失败条目（历史不断裂）。
- 执行成功 → 历史照常写入，无失败回填。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, create_engine

from bycall.agent.im_handler import handle_im_message
from bycall.agent.runner import AgentResult
from bycall.agent.session_lease import acquire_turn_lease, current_holder, release_turn_lease
from bycall.agent.session_store import get_or_create_conversation, load_message_history
from bycall.db import init_db, session_scope, set_engine
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.governance.models import Conversation, User


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    """干净的 SQLite + init_db（建表含 sessionlease / conversation / message）。"""
    db_path = tmp_path / "test_robust.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    return Session(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))


@pytest.fixture
def user_id(db) -> int:
    """建一个真实 User，供 ``_run_stream`` 的 ``session.get(User, id)`` 命中。"""
    with session_scope() as s:
        u = User(name="imuser1", display_name="员工", role="member", is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _answered_result() -> AgentResult:
    return AgentResult(
        answer="基于知识库的回答。",
        decision="answered",
        knowledge_scope=["public"],
        retrieved_chunks=1,
        user_name="u",
        role="staff",
        tool_calls=[],
    )


def _streaming_sender(cap: list) -> ReplySender:
    async def send_text(content):
        cap.append(("text", content))

    async def send_stream(content, stream_id, finish, msg_item=None):
        cap.append(("stream", content, stream_id, finish))

    return ReplySender(
        send_text=send_text, send_stream=send_stream, send_card=None, channel="wecom"
    )


def _text_msg(text: str = "公司介绍") -> InboundMessage:
    return InboundMessage(channel="wecom", im_user_id="u1", text=text, raw_frame={})


def _drive(msg: InboundMessage, sender: ReplySender, fake_run_agent_stream, user_id: int) -> None:
    """以真实 ``_run_stream`` 驱动 handle_im_message，仅桩外部依赖。"""
    limiter = MagicMock()
    limiter.allow.return_value = True
    user = MagicMock(id=user_id)
    with (
        patch("bycall.agent.im_handler.resolve_user_by_im", return_value=user),
        patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
        patch("bycall.llm.resolver.get_chat_model", return_value=(MagicMock(), MagicMock())),
        patch("bycall.agent.runner.run_agent_stream", fake_run_agent_stream),
    ):
        asyncio.run(handle_im_message(msg, sender))


def _find_conv_id(user_id: int) -> int | None:
    """查用户会话 id（在 session 内读取，避免退出后实例过期）。"""
    with session_scope() as s:
        conv = s.exec(Conversation.select_by_session_key(f"wecom:{user_id}")).first()
        return conv.id if conv is not None else None


# ============ busy：会话被占用 → 提示稍后重试，不执行治理链路 ============


def test_busy_result_when_session_occupied(db: Session, user_id: int):
    """会话被租约占用 → _busy_result（提示稍后重试），不执行治理链路。"""
    # 先建会话并持租约，模拟「上一条消息正在处理中」。
    with session_scope() as s:
        conv = get_or_create_conversation(s, user_id, "wecom")
        conv_id = conv.id
    token = acquire_turn_lease(conv_id, ttl_seconds=60)
    assert token

    called: list = []
    cap: list = []
    sender = _streaming_sender(cap)

    async def fake_run_agent_stream(**kw):
        called.append(True)
        yield "", True, _answered_result()

    _drive(_text_msg(), sender, fake_run_agent_stream, user_id)

    # 治理链路未执行
    assert not called
    # 提示稍后重试
    assert any(c[0] == "stream" and "请稍后再试" in str(c[1]) for c in cap)
    # 占用者的租约未被释放（仍是持有者）
    with session_scope() as s:
        holder = current_holder(s, conv_id)
        assert holder is not None and holder.token == token
    release_turn_lease(conv_id, token)  # 清理


# ============ turn 结束租约释放（正常 / 异常）+ 成功历史 ============


def test_success_path_history_and_lease_released(db: Session, user_id: int):
    """执行成功：历史照常写入（user+assistant），无失败回填；turn 结束租约释放。"""
    cap: list = []
    sender = _streaming_sender(cap)

    async def fake_run_agent_stream(
        *, session, user, question, llm, channel="api", conversation_id=None, im_user_id=""
    ):
        # 模拟真实 run_agent_stream 的正常写入：user + assistant 两条。
        from bycall.agent.session_store import save_message

        save_message(session, conversation_id, "user", question)
        save_message(session, conversation_id, "assistant", "基于知识库的回答。")
        session.commit()
        yield "基于知识库的回答。", True, _answered_result()

    _drive(_text_msg("公司介绍"), sender, fake_run_agent_stream, user_id)

    conv_id = _find_conv_id(user_id)
    assert conv_id is not None
    with session_scope() as s:
        roles = [m.role for m in load_message_history(s, conv_id)]
        # 成功路径：user + assistant 照常写入，无 system 失败条目
        assert roles == ["user", "assistant"]
        # turn 正常结束，租约已释放
        assert current_holder(s, conv_id) is None


def test_failure_backfills_history_and_releases_lease(db: Session, user_id: int):
    """执行失败（run_agent_stream 中途异常）：user 消息已持久化 + system 失败条目；租约释放。"""
    cap: list = []
    sender = _streaming_sender(cap)

    async def fake_run_agent_stream(**kw):
        # 先产出一个非 final chunk，再抛异常 → 忠实模拟「流式中途失败」。
        yield "", False, None
        raise RuntimeError("治理链路崩溃")

    _drive(_text_msg("公司介绍"), sender, fake_run_agent_stream, user_id)

    conv_id = _find_conv_id(user_id)
    assert conv_id is not None
    with session_scope() as s:
        hist = load_message_history(s, conv_id)
        roles = [m.role for m in hist]
        contents = [m.content for m in hist]
        # 历史不断裂：user 问题已持久化 + 一条 system 失败条目
        assert roles == ["user", "system"]
        assert contents[0] == "公司介绍"
        assert "处理失败" in contents[1]
        # turn 异常结束，租约同样释放
        assert current_holder(s, conv_id) is None
    # 异常路径也给了错误提示帧（非空白）
    assert any(c[0] == "stream" and "出错" in str(c[1]) for c in cap)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
