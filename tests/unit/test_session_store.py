"""会话存储 + 上下文构建 + 压缩测试（参考 Hermes SessionStore + ContextCompressor）。

覆盖：
- get_or_create_conversation: 首次创建 / 二次查到同一条
- save_message + load_message_history: 存取消息 + 滑窗
- build_conversation_messages: system(prompt+memory+summary) + history + rag + user
- should_compress: 阈值判断
- compress_history: LLM 增量摘要（mock LLM）
- UserMemory: 加载用户记忆注入 system message

不连真实 LLM，用 mock ChatModel。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, create_engine

from bycall.agent.session_store import (
    build_conversation_messages,
    compress_history,
    get_or_create_conversation,
    load_message_history,
    load_user_memory,
    save_message,
    should_compress,
)
from bycall.db import init_db, set_engine
from bycall.governance.models import UserMemory


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    """干净的 SQLite + init_db（建表 + 种子）。"""
    db_path = tmp_path / "test_session.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return Session(engine)


# ============ get_or_create_conversation ============


class TestGetOrCreateConversation:
    def test_creates_new_conversation(self, db: Session):
        """首次调用创建会话，session_key = {channel}:{user_id}。"""
        conv = get_or_create_conversation(db, user_id=1, channel="api")
        assert conv.id is not None
        assert conv.session_key == "api:1"
        assert conv.user_id == 1
        assert conv.channel == "api"
        assert conv.message_count == 0
        assert conv.summary == ""

    def test_second_call_returns_same_conversation(self, db: Session):
        """二次调用返回同一条会话（不创建新的）。"""
        conv1 = get_or_create_conversation(db, user_id=1, channel="api")
        conv2 = get_or_create_conversation(db, user_id=1, channel="api")
        assert conv1.id == conv2.id

    def test_different_channels_different_conversations(self, db: Session):
        """不同渠道 = 不同会话（api vs wecom）。"""
        conv_api = get_or_create_conversation(db, user_id=1, channel="api")
        conv_wecom = get_or_create_conversation(db, user_id=1, channel="wecom")
        assert conv_api.id != conv_wecom.id
        assert conv_api.session_key == "api:1"
        assert conv_wecom.session_key == "wecom:1"

    def test_different_users_different_conversations(self, db: Session):
        """不同用户 = 不同会话。"""
        conv_a = get_or_create_conversation(db, user_id=1, channel="api")
        conv_b = get_or_create_conversation(db, user_id=2, channel="api")
        assert conv_a.id != conv_b.id


# ============ save_message + load_message_history ============


class TestMessagePersistence:
    def test_save_and_load_history(self, db: Session):
        """存消息后能按时间正序加载。"""
        conv = get_or_create_conversation(db, user_id=1, channel="api")

        save_message(db, conv.id, "user", "你好")
        save_message(db, conv.id, "assistant", "你好，有什么可以帮您？")
        db.commit()

        history = load_message_history(db, conv.id)
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "你好"
        assert history[1].role == "assistant"
        assert history[1].content == "你好，有什么可以帮您？"

    def test_message_count_updates(self, db: Session):
        """存消息后 conversation.message_count 递增。"""
        conv = get_or_create_conversation(db, user_id=1, channel="api")
        save_message(db, conv.id, "user", "问题1")
        save_message(db, conv.id, "assistant", "回答1")
        db.commit()

        db.refresh(conv)
        assert conv.message_count == 2

    def test_history_limit(self, db: Session):
        """load_message_history 只返回最近 N 条。"""
        conv = get_or_create_conversation(db, user_id=1, channel="api")
        for i in range(10):
            save_message(db, conv.id, "user", f"问题{i}")
            save_message(db, conv.id, "assistant", f"回答{i}")
        db.commit()

        history = load_message_history(db, conv.id, limit=5)
        assert len(history) == 5
        # 应该是最后 5 条（最新 2 轮 + 1 条）
        assert "问题9" in history[-1].content or "回答9" in history[-1].content


# ============ build_conversation_messages ============


class TestBuildConversationMessages:
    def test_single_turn_no_history(self):
        """无历史时：system + rag + user 三条。"""
        messages = build_conversation_messages(
            system_prompt="你是数字员工。",
            rag_context="[sales] 报价流程",
            question="报价流程是什么？",
            history=[],
            summary="",
        )
        # system + rag + user = 3 条
        assert len(messages) == 3
        assert messages[0].role == "system"
        assert "你是数字员工" in messages[0].content
        assert messages[1].role == "system"
        assert "报价流程" in messages[1].content
        assert messages[2].role == "user"
        assert messages[2].content == "报价流程是什么？"

    def test_with_history(self):
        """有历史时：system + rag + history + user。"""
        from bycall.governance.models import Message as Msg

        history = [
            Msg(conversation_id=1, role="user", content="之前的问"),
            Msg(conversation_id=1, role="assistant", content="之前的答"),
        ]
        messages = build_conversation_messages(
            system_prompt="你是数字员工。",
            rag_context="",
            question="新问题",
            history=history,
            summary="",
        )
        # system + (no rag) + 2 history + user = 4 条
        assert len(messages) == 4
        assert messages[1].role == "user"
        assert messages[1].content == "之前的问"
        assert messages[2].role == "assistant"
        assert messages[2].content == "之前的答"
        assert messages[3].role == "user"
        assert messages[3].content == "新问题"

    def test_summary_injected_into_system(self):
        """summary 作为 system message 注入（不是 user message）。"""
        messages = build_conversation_messages(
            system_prompt="你是数字员工。",
            rag_context="",
            question="问题",
            history=[],
            summary="之前讨论了报价流程",
        )
        assert len(messages) == 2  # system + user
        assert "对话摘要" in messages[0].content
        assert "之前讨论了报价流程" in messages[0].content

    def test_user_memory_injected_into_system(self):
        """用户记忆作为 system message 注入。"""
        from bycall.governance.models import UserMemory as Mem

        memories = [Mem(user_id=1, category="preference", content="喜欢简洁回答")]
        messages = build_conversation_messages(
            system_prompt="你是数字员工。",
            rag_context="",
            question="问题",
            history=[],
            summary="",
            user_memories=memories,
        )
        assert "用户记忆" in messages[0].content
        assert "喜欢简洁回答" in messages[0].content
        assert "preference" in messages[0].content


# ============ should_compress ============


class TestShouldCompress:
    def test_short_history_no_compress(self):
        """短历史不压缩。"""
        from bycall.governance.models import Message as Msg

        history = [Msg(conversation_id=1, role="user", content="短", token_count=1)]
        assert should_compress(history) is False

    def test_below_threshold_no_compress(self):
        """token 总和低于阈值不压缩。"""
        from bycall.governance.models import Message as Msg

        # 12 条消息，每条 token_count=1，远低于 4000 阈值
        history = [
            Msg(conversation_id=1, role="user" if i % 2 == 0 else "assistant", content="短", token_count=1)
            for i in range(12)
        ]
        assert should_compress(history) is False

    def test_above_threshold_triggers_compress(self):
        """token 总和超阈值 + 足够长时触发压缩。"""
        from bycall.governance.models import Message as Msg

        # 20 条消息，每条 token_count=300 → 总 6000 > 4000 阈值
        history = [
            Msg(conversation_id=1, role="user" if i % 2 == 0 else "assistant", content="x" * 900, token_count=300)
            for i in range(20)
        ]
        assert should_compress(history) is True


# ============ compress_history ============


class TestCompressHistory:
    def test_compress_returns_summary(self):
        """压缩返回 LLM 生成的摘要。"""
        from bycall.governance.models import Message as Msg

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "用户讨论了报价流程和审批步骤。"

        history = [
            Msg(conversation_id=1, role="user" if i % 2 == 0 else "assistant", content=f"内容{i}" * 100)
            for i in range(20)
        ]

        result = compress_history(mock_llm, history, previous_summary="")
        assert result == "用户讨论了报价流程和审批步骤。"
        # LLM 被调用了一次
        mock_llm.chat.assert_called_once()

    def test_compress_with_previous_summary(self):
        """有 previous_summary 时传给 LLM 做增量更新。"""
        from bycall.governance.models import Message as Msg

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "更新后的摘要"

        history = [
            Msg(conversation_id=1, role="user" if i % 2 == 0 else "assistant", content=f"内容{i}" * 100)
            for i in range(20)
        ]

        compress_history(mock_llm, history, previous_summary="之前的摘要")
        # 验证 LLM 收到的 prompt 里包含之前的摘要
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]
        assert "之前的摘要" in messages[0].content

    def test_compress_failure_returns_previous(self):
        """LLM 调用失败时返回 previous_summary（降级，不丢已有摘要）。"""
        from bycall.governance.models import Message as Msg

        mock_llm = MagicMock()
        mock_llm.chat.side_effect = Exception("LLM 不可用")

        history = [
            Msg(conversation_id=1, role="user" if i % 2 == 0 else "assistant", content=f"内容{i}" * 100)
            for i in range(20)
        ]

        result = compress_history(mock_llm, history, previous_summary="已有摘要")
        assert result == "已有摘要"

    def test_short_history_no_compress(self):
        """历史短于 keep_recent 时直接返回 previous_summary。"""
        mock_llm = MagicMock()
        history = []  # 空历史
        result = compress_history(mock_llm, history, previous_summary="旧摘要")
        assert result == "旧摘要"
        mock_llm.chat.assert_not_called()


# ============ UserMemory ============


class TestUserMemory:
    def test_load_user_memory(self, db: Session):
        """加载用户记忆。"""
        db.add(UserMemory(user_id=1, category="preference", content="喜欢简洁"))
        db.add(UserMemory(user_id=1, category="fact", content="负责华东区"))
        db.add(UserMemory(user_id=2, category="note", content="另一个用户"))
        db.commit()

        mems = load_user_memory(db, user_id=1)
        assert len(mems) == 2
        assert all(m.user_id == 1 for m in mems)
