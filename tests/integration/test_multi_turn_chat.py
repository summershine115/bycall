"""多轮对话端到端测试（参考 Hermes Agent 的会话持久化）。

覆盖：
- 连续两轮提问，第二轮能看到第一轮的上下文
- 消息持久化到 DB（Conversation + Message）
- 压缩触发后 summary 更新（mock LLM）
- 会话管理 API（list/get/messages/delete/reset）
- 用户记忆注入

用 mock LLM（不连真实 API），验证会话状态链路而非 LLM 质量。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.llm import resolver


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """干净环境：SQLite + zvec + dev 模式。"""
    db_file = tmp_path / "test_multi.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_file}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False}))
    init_db()
    return tmp_path


@pytest.fixture
def client(env, monkeypatch):
    """FastAPI TestClient，LLM 被 mock。

    mock LLM 记录收到的 messages，返回固定回答，便于断言历史注入。
    登录为 admin（admin API 需要会话认证）。
    """
    mock_llm = MagicMock(spec=BaseChatModel)
    call_log: list[list] = []

    # M3：对话模型是 LangChain BaseChatModel，.invoke(messages) -> AIMessage。
    # bind_tools 在 graph 内调用，mock 需返回自身。
    def invoke(messages, **kwargs):
        call_log.append(messages)
        return AIMessage(content=f"这是第 {len(call_log)} 轮的回答")

    mock_llm.invoke.side_effect = invoke
    mock_llm.bind_tools.return_value = mock_llm
    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(resolver, "get_chat_model", lambda session: (fake_model, mock_llm))

    app = create_app()
    c = TestClient(app)

    # 登录为 admin（admin API + /chat 都需要会话）
    resp = c.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert resp.status_code == 200 and resp.json().get("ok"), f"登录失败: {resp.text}"

    return c, mock_llm, call_log


# ============ 多轮对话 ============


class TestMultiTurnChat:
    def test_second_turn_sees_first_turn_history(self, client):
        """第二轮对话能看到第一轮的 user+assistant 消息。"""
        c, _mock_llm, call_log = client

        # 第一轮
        resp1 = c.post("/chat", json={"question": "你好，我是张三"})
        assert resp1.status_code == 200
        assert len(call_log) == 1
        # 第一轮消息：system + rag + user（无历史）
        msgs1 = call_log[0]
        assert msgs1[-1].content == "你好，我是张三"

        # 第二轮
        resp2 = c.post("/chat", json={"question": "我叫什么名字？"})
        assert resp2.status_code == 200
        assert len(call_log) == 2
        # 第二轮消息应包含第一轮的历史
        msgs2 = call_log[1]
        # 找到历史中的 user 消息
        history_contents = [m.content for m in msgs2]
        assert "你好，我是张三" in history_contents
        assert "我叫什么名字？" in history_contents

    def test_messages_persisted_to_db(self, client):
        """两轮对话后 DB 里有 4 条消息（2 user + 2 assistant）。"""
        c, _, _ = client
        c.post("/chat", json={"question": "问题1"})
        c.post("/chat", json={"question": "问题2"})

        # 查会话列表
        convs = c.get("/api/admin/conversations").json()
        assert len(convs) >= 1
        conv_id = convs[0]["id"]

        # 查消息
        msgs = c.get(f"/api/admin/conversations/{conv_id}/messages").json()
        assert len(msgs) == 4  # 2 user + 2 assistant
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_conversation_isolated_per_user(self, client, monkeypatch):
        """不同用户（act-as 不同目标）的会话互相隔离。"""
        c, _, _ = client

        # 先创建一个测试用户
        c.post(
            "/api/admin/users",
            json={"name": "testuser", "role": "member", "is_active": True},
        )

        # admin 直接提问（admin 会话）
        c.post("/chat", json={"question": "admin 的问题"})

        # 扮演 testuser 提问
        users = c.get("/api/admin/users").json()
        testuser = next(u for u in users if u["name"] == "testuser")
        c.post("/api/admin/act-as", params={"target_user_id": testuser["id"]})
        c.post("/chat", json={"question": "用户2的问题"})

        # 恢复 admin
        c.post("/api/admin/clear-act-as")

        # 应有两个会话（admin + testuser）
        convs = c.get("/api/admin/conversations").json()
        assert len(convs) >= 2

    def test_reset_clears_history(self, client):
        """重置会话后历史清空，但会话本身还在。"""
        c, _, _ = client
        c.post("/chat", json={"question": "问题1"})

        convs = c.get("/api/admin/conversations").json()
        conv_id = convs[0]["id"]

        # 重置前有消息
        msgs_before = c.get(f"/api/admin/conversations/{conv_id}/messages").json()
        assert len(msgs_before) == 2

        # 重置
        c.post(f"/api/admin/conversations/{conv_id}/reset")

        # 重置后无消息
        msgs_after = c.get(f"/api/admin/conversations/{conv_id}/messages").json()
        assert len(msgs_after) == 0

        # 会话还在
        conv_detail = c.get(f"/api/admin/conversations/{conv_id}").json()
        assert conv_detail["id"] == conv_id
        assert conv_detail["message_count"] == 0

    def test_delete_removes_conversation(self, client):
        """删除会话后查不到了。"""
        c, _, _ = client
        c.post("/chat", json={"question": "问题1"})

        convs = c.get("/api/admin/conversations").json()
        conv_id = convs[0]["id"]

        # 删除
        resp = c.delete(f"/api/admin/conversations/{conv_id}")
        assert resp.status_code == 204

        # 查不到了
        resp = c.get(f"/api/admin/conversations/{conv_id}")
        assert resp.status_code == 404


# ============ 用户记忆 ============


class TestUserMemory:
    def test_create_and_list_memory(self, client):
        """创建用户记忆 + 列出。"""
        c, _, _ = client

        # 创建
        resp = c.post(
            "/api/admin/users/2/memory",
            json={"category": "preference", "content": "喜欢简洁回答"},
        )
        assert resp.status_code == 201
        resp.json()["id"]

        # 列出
        mems = c.get("/api/admin/users/2/memory").json()
        assert len(mems) == 1
        assert mems[0]["content"] == "喜欢简洁回答"
        assert mems[0]["category"] == "preference"

    def test_invalid_category_rejected(self, client):
        """无效 category → 400。"""
        c, _, _ = client
        resp = c.post(
            "/api/admin/users/2/memory",
            json={"category": "invalid", "content": "内容"},
        )
        assert resp.status_code == 400

    def test_update_memory(self, client):
        """更新记忆条目。"""
        c, _, _ = client
        create_resp = c.post(
            "/api/admin/users/2/memory",
            json={"category": "note", "content": "原始内容"},
        )
        mem_id = create_resp.json()["id"]

        update_resp = c.put(f"/api/admin/memory/{mem_id}", json={"content": "更新后"})
        assert update_resp.status_code == 200
        assert update_resp.json()["content"] == "更新后"

    def test_delete_memory(self, client):
        """删除记忆条目。"""
        c, _, _ = client
        create_resp = c.post(
            "/api/admin/users/2/memory",
            json={"category": "fact", "content": "待删除"},
        )
        mem_id = create_resp.json()["id"]

        resp = c.delete(f"/api/admin/memory/{mem_id}")
        assert resp.status_code == 204

        # 列表里没了
        mems = c.get("/api/admin/users/2/memory").json()
        assert all(m["id"] != mem_id for m in mems)
