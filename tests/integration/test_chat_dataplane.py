"""数据面 /chat 端点扩展测试：治理透明化字段 + 流式 SSE + 会话历史 + 重置。

覆盖本次「对话测试升级」新增的后端契约：
- /chat 响应透传 tool_calls / citations / context_trace / conversation_id
- /chat/stream SSE 逐 token + done 事件
- /chat/history 当前用户 api 会话历史
- /chat/reset 清空会话（保留会话本身）

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
        # answered 时 context_trace 非空（含 system/RAG 快照）
        assert data["decision"] == "answered"
        assert data["context_trace"] != ""


class TestChatHistory:
    def test_history_empty_before_chat(self, client):
        """发消息前 history 返回空（无会话）。"""
        resp = client.get("/chat/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversation_id"] is None
        assert data["messages"] == []

    def test_history_returns_messages_after_chat(self, client):
        """一轮对话后 history 返回 user + assistant 两条消息。"""
        client.post("/chat", json={"question": "报销流程是什么"})
        resp = client.get("/chat/history")
        data = resp.json()
        assert data["conversation_id"] is not None
        roles = [m["role"] for m in data["messages"]]
        assert roles == ["user", "assistant"]
        assert data["messages"][0]["content"] == "报销流程是什么"
        assert data["messages"][1]["content"] == "测试回答"


class TestChatReset:
    def test_reset_clears_history(self, client):
        """reset 清空消息但保留会话本身。"""
        client.post("/chat", json={"question": "问题一"})
        assert len(client.get("/chat/history").json()["messages"]) == 2

        resp = client.post("/chat/reset")
        assert resp.status_code == 200
        assert resp.json()["reset"] is True

        data = client.get("/chat/history").json()
        assert data["messages"] == []
        # 会话保留（同一 conversation_id）
        assert data["conversation_id"] is not None

    def test_reset_when_no_conversation(self, client):
        """无会话时 reset 返回 reset=False（不报错）。"""
        resp = client.post("/chat/reset")
        assert resp.status_code == 200
        assert resp.json()["reset"] is False


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
        # 两个 token 事件
        assert text.count("event: token") == 2
        # done 事件含完整 ChatResponse
        assert "event: done" in text
        done = _parse_sse_done(text)
        assert done is not None
        assert done["answer"] == "你好"
        assert done["conversation_id"] is not None
        assert done["decision"] == "answered"
        assert done["context_trace"] != ""

    def test_stream_denied_no_access_uses_done_event(self, client, monkeypatch):
        """无知识域权限的用户 → done 事件带 denied_no_access（仍走 SSE，非 HTTP 错误）。"""
        # patch runner 顶部 from-import 的 compute_capability_set 本地引用（非源模块），
        # 让 admin 无知识域 → denied_no_access 路径。monkeypatch 自动恢复。
        empty_cap = SimpleNamespace(
            role="admin",
            knowledge_scope_tags=[],
            visible_kb_ids=[],
            is_admin=False,
            allowed_tools=[],
            on_demand_skills=[],
            skills=[],
            skill_markdown="",
            knowledge_scope_text="",
            system_prompt="sys",
        )

        monkeypatch.setattr(
            "bycall.agent.runner.compute_capability_set",
            lambda session, user: empty_cap,
        )
        resp = client.post("/chat/stream", json={"question": "你好"})
        assert resp.status_code == 200
        done = _parse_sse_done(resp.text)
        assert done is not None
        assert done["decision"] == "denied_no_access"
