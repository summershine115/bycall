"""数据面 /chat 端点测试：治理透明化字段 + 流式 SSE + 多会话（列表/切换/删除）。

覆盖：
- /chat 响应透传 tool_calls / citations / context_trace / conversation_id
- /chat/stream SSE 逐 token + done 事件
- api 渠道多会话：不带 conversation_id 新建、带 id 继续、列表、消息、删除、归属校验

用 mock LLM（不连真实 API），验证 HTTP 契约而非 LLM 质量。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlmodel import create_engine

from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.llm import resolver


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """干净环境：SQLite + zvec + dev 模式。"""
    db_file = tmp_path / "test_dp.db"
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
    """FastAPI TestClient，LLM 被 mock（同步 invoke + 流式 astream 都覆盖）。"""
    mock_llm = MagicMock(spec=BaseChatModel)
    mock_llm.invoke.side_effect = lambda messages, **kw: AIMessage(content="测试回答")
    mock_llm.bind_tools.return_value = mock_llm

    async def _astream(messages, **kw):
        for t in ["你", "好"]:
            yield AIMessageChunk(content=t)

    mock_llm.astream = _astream

    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(resolver, "get_chat_model", lambda session: (fake_model, mock_llm))

    app = create_app()
    c = TestClient(app)
    resp = c.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert resp.status_code == 200 and resp.json().get("ok"), f"登录失败: {resp.text}"
    return c


def _parse_sse_done(text: str) -> dict | None:
    """从 SSE 文本里提取 done 事件的 JSON data。"""
    for block in text.split("\n\n"):
        if block.startswith("event: done"):
            data_line = block.split("data: ", 1)[1]
            return json.loads(data_line)
    return None


class TestChatTransparency:
    def test_chat_returns_governance_fields(self, client):
        """/chat 响应含治理透明化字段（tool_calls/citations/context_trace/conversation_id）。"""
        resp = client.post("/chat", json={"question": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        for field in ("tool_calls", "citations", "context_trace", "conversation_id"):
            assert field in data, f"缺少字段 {field}"
        assert data["conversation_id"] is not None
        assert isinstance(data["tool_calls"], list)
        assert isinstance(data["citations"], list)
        assert data["decision"] == "answered"
        assert data["context_trace"] != ""


class TestMultiConversation:
    def test_first_chat_creates_conversation(self, client):
        """不带 conversation_id → 新建会话，返回新 conversation_id。"""
        resp = client.post("/chat", json={"question": "你好"})
        assert resp.status_code == 200
        assert resp.json()["conversation_id"] is not None

    def test_continue_same_conversation(self, client):
        """带 conversation_id → 继续该会话，返回同一 id。"""
        cid = client.post("/chat", json={"question": "第一问"}).json()["conversation_id"]
        resp = client.post("/chat", json={"question": "第二问", "conversation_id": cid})
        assert resp.json()["conversation_id"] == cid

    def test_two_chats_create_distinct_conversations(self, client):
        """两次不带 conv_id → 两个不同会话（多会话支持）。"""
        c1 = client.post("/chat", json={"question": "话题A"}).json()["conversation_id"]
        c2 = client.post("/chat", json={"question": "话题B"}).json()["conversation_id"]
        assert c1 != c2

    def test_conversation_list_with_preview(self, client):
        """会话列表含已创建会话，preview 取首条 user 消息。"""
        cid = client.post("/chat", json={"question": "列表测试问题"}).json()["conversation_id"]
        resp = client.get("/chat/conversations")
        assert resp.status_code == 200
        item = next((c for c in resp.json() if c["id"] == cid), None)
        assert item is not None
        assert item["preview"] == "列表测试问题"
        assert item["message_count"] == 2  # user + assistant

    def test_conversation_messages(self, client):
        """加载某会话的消息历史（切换会话用）。"""
        cid = client.post("/chat", json={"question": "历史问题"}).json()["conversation_id"]
        resp = client.get(f"/chat/conversations/{cid}/messages")
        assert resp.status_code == 200
        roles = [m["role"] for m in resp.json()]
        assert roles == ["user", "assistant"]
        assert resp.json()[0]["content"] == "历史问题"

    def test_delete_conversation(self, client):
        """删除会话后列表不含、消息不可查。"""
        cid = client.post("/chat", json={"question": "待删除"}).json()["conversation_id"]
        assert client.delete(f"/chat/conversations/{cid}").status_code == 204
        assert cid not in [c["id"] for c in client.get("/chat/conversations").json()]
        assert client.get(f"/chat/conversations/{cid}/messages").status_code == 404

    def test_cannot_access_nonexistent_conversation(self, client):
        """访问不存在的会话 → 404（消息查询 + 继续对话均拦截）。"""
        assert client.get("/chat/conversations/99999/messages").status_code == 404
        resp = client.post("/chat", json={"question": "x", "conversation_id": 99999})
        assert resp.status_code == 404


class TestChatStream:
    def test_stream_requires_login(self, env, monkeypatch):
        """未登录 → 401（认证在进入流之前完成）。"""
        mock_llm = MagicMock(spec=BaseChatModel)
        mock_llm.bind_tools.return_value = mock_llm
        monkeypatch.setattr(resolver, "get_chat_model", lambda s: (SimpleNamespace(id=1), mock_llm))
        c = TestClient(create_app())
        resp = c.post("/chat/stream", json={"question": "你好"})
        assert resp.status_code == 401

    def test_stream_returns_sse_with_done_payload(self, client):
        """流式返回 SSE：token 事件逐块 + done 事件带治理元数据。"""
        resp = client.post("/chat/stream", json={"question": "你好"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text
        assert text.count("event: token") == 2
        assert "event: done" in text
        done = _parse_sse_done(text)
        assert done is not None
        assert done["answer"] == "你好"
        assert done["conversation_id"] is not None
        assert done["decision"] == "answered"
        assert done["context_trace"] != ""

    def test_stream_continue_conversation(self, client):
        """流式带 conversation_id → 继续同一会话。"""
        cid = client.post("/chat", json={"question": "第一问"}).json()["conversation_id"]
        resp = client.post("/chat/stream", json={"question": "第二问", "conversation_id": cid})
        assert resp.status_code == 200
        done = _parse_sse_done(resp.text)
        assert done["conversation_id"] == cid
