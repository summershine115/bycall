"""入站消息幂等去重单测——DB 持久去重 / inflight 去重 / make_key / retention 清理 / 集成重发。

防的真实 bug：企微重连/重发同一 ``MsgId`` 导致双执行（双回答 / 双费用 / 双审计）、
进程重启后 inflight 丢失导致已处理消息被重复执行、幂等表随消息量无限增长。
全部跑在真实 SQLite 上，验证 DB 层的 once 语义与 retention 清理。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, create_engine

from bycall.agent.idempotency import (
    claim_message,
    make_key,
    mark_message_done,
    prune_idempotency_records,
    release_inflight,
)
from bycall.agent.im_handler import handle_im_message
from bycall.agent.runner import AgentResult
from bycall.db import init_db, session_scope, set_engine
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.governance.models import IdempotencyRecord


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    """干净的 SQLite + init_db（建表含 idempotencyrecord）。"""
    db_path = tmp_path / "test_idem.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    return Session(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))


# ============ DB 持久化去重 ============


class TestClaimPersistentDedup:
    def test_first_claim_true_second_false(self, db: Session):
        """首次认领 True，同键再次认领 False（DB 持久层去重）。"""
        assert claim_message(db, "wecom:M1") is True
        # 完整处理流程：先落 done 记录、再清 inflight（与 handle_im_message 的 finally 一致）。
        mark_message_done(db, "wecom:M1")
        release_inflight("wecom:M1")
        db.commit()
        # 同键再次认领 → DB 已有 done 记录 → 重复
        assert claim_message(db, "wecom:M1") is False
        assert db.exec(IdempotencyRecord.select_by_key("wecom:M1")).first() is not None

    def test_key_scoped_by_channel(self, db: Session):
        """不同渠道同 msg_id 不冲突（幂等键含渠道前缀）。"""
        assert claim_message(db, "wecom:M1") is True
        mark_message_done(db, "wecom:M1")
        release_inflight("wecom:M1")
        db.commit()
        # 另一渠道首次到达 → 放行（渠道前缀隔离）
        assert claim_message(db, "feishu:M1") is True
        mark_message_done(db, "feishu:M1")
        release_inflight("feishu:M1")
        db.commit()


# ============ inflight 去重（处理中） ============


class TestInflightDedup:
    def test_inflight_blocks_reentrant_claim(self, db: Session):
        """处理中（已 claim 未 done）再次 claim False；释放后可重试；done 后永久重复。"""
        assert claim_message(db, "wecom:M2") is True
        # 处理中 → 重复
        assert claim_message(db, "wecom:M2") is False
        # 瞬态拒绝（限流等）未落 done，释放后可重新认领
        release_inflight("wecom:M2")
        assert claim_message(db, "wecom:M2") is True
        release_inflight("wecom:M2")
        # done 后 → DB 去重，永久重复
        mark_message_done(db, "wecom:M2")
        db.commit()
        assert claim_message(db, "wecom:M2") is False

    def test_done_then_claim_is_duplicate(self, db: Session):
        """同键 done 后仍是重复（once 语义：已处理完毕的消息重发不再执行）。"""
        assert claim_message(db, "wecom:M3") is True
        release_inflight("wecom:M3")
        mark_message_done(db, "wecom:M3")
        db.commit()
        assert claim_message(db, "wecom:M3") is False


# ============ make_key ============


class TestMakeKey:
    def test_none_when_empty_msg_id(self):
        """msg_id 为空 → 返回 None（调用方放行，无法去重）。"""
        assert make_key("wecom", "") is None
        assert make_key("wecom", None) is None

    def test_compound_key_with_channel(self):
        assert make_key("wecom", "abc") == "wecom:abc"


# ============ retention 清理 ============


class TestPrune:
    def test_deletes_expired_keeps_fresh(self, db: Session):
        """删除超期记录、保留未超期（按 retention 天数）。"""
        now = datetime.now(UTC)
        db.add(IdempotencyRecord(key="wecom:old", created_at=now - timedelta(days=30)))
        db.add(IdempotencyRecord(key="wecom:fresh", created_at=now))
        db.commit()
        deleted = prune_idempotency_records(db, retention_days=14)
        db.commit()
        assert deleted == 1
        assert db.exec(IdempotencyRecord.select_by_key("wecom:old")).first() is None
        assert db.exec(IdempotencyRecord.select_by_key("wecom:fresh")).first() is not None

    def test_no_delete_within_retention(self, db: Session):
        """retention 内（未超期）不删除。"""
        now = datetime.now(UTC)
        db.add(IdempotencyRecord(key="wecom:recent", created_at=now - timedelta(days=2)))
        db.commit()
        assert prune_idempotency_records(db, retention_days=14) == 0
        db.commit()
        assert db.exec(IdempotencyRecord.select_by_key("wecom:recent")).first() is not None


# ============ 集成：handle_im_message 同 msg_id 只执行一次 ============


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


def test_handle_im_message_same_msgid_executes_once(db: Session):
    """企微重连/重发同一 MsgId → 只执行一次（模拟双投递）。"""
    calls: list[str] = []

    async def fake_run_stream(user_id, question, channel, im_user_id="", agent_id=None):
        calls.append(question)
        yield "", True, _answered_result()

    cap: list = []
    sender = _streaming_sender(cap)
    msg = InboundMessage(
        channel="wecom", im_user_id="u1", text="公司介绍", raw_frame={}, msg_id="M100"
    )
    limiter = MagicMock()
    limiter.allow.return_value = True
    user = MagicMock(id=7)
    with (
        patch("bycall.agent.im_handler.resolve_user_by_im", return_value=user),
        patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
        patch("bycall.agent.im_handler._run_stream", fake_run_stream),
    ):
        asyncio.run(handle_im_message(msg, sender))
        # 同 msg_id 重发（模拟企微重连重放）
        asyncio.run(handle_im_message(msg, sender))

    assert calls == ["公司介绍"]  # 只执行一次
    # 处理完毕已落 done 记录（防进程重启后 inflight 丢失仍被重放）
    with session_scope() as s:
        assert s.exec(IdempotencyRecord.select_by_key("wecom:M100")).first() is not None


def test_handle_im_message_no_msgid_always_runs(db: Session):
    """无 msg_id（事件回执等）→ 不做幂等，每条都执行。"""
    calls: list = []

    async def fake_run_stream(user_id, question, channel, im_user_id="", agent_id=None):
        calls.append(question)
        yield "", True, _answered_result()

    cap: list = []
    sender = _streaming_sender(cap)
    msg = InboundMessage(channel="wecom", im_user_id="u1", text="你好", raw_frame={})  # 无 msg_id
    limiter = MagicMock()
    limiter.allow.return_value = True
    user = MagicMock(id=7)
    with (
        patch("bycall.agent.im_handler.resolve_user_by_im", return_value=user),
        patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
        patch("bycall.agent.im_handler._run_stream", fake_run_stream),
    ):
        asyncio.run(handle_im_message(msg, sender))
        asyncio.run(handle_im_message(msg, sender))

    assert len(calls) == 2  # 无 msg_id 不去重，两次都执行


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
