"""LLM 抽象测试：factory 路由 + chat/embed 端到端（用 mock client，不连真实 API）。

覆盖：
- factory 按 provider 正确实例化（openai / glm）
- OpenAICompatClient.chat 走通（mock SDK 响应）
- OpenAICompatClient.embed 返回对齐的向量
- GLMClient 复用 OpenAI 兼容协议（仅默认 base_url 不同）
- LLMClient 抽象的 async 兜底
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bycall.config import LLMConfig
from bycall.llm.base import LLMClient, Message
from bycall.llm.factory import create_llm_client
from bycall.llm.glm import GLM_DEFAULT_BASE_URL, GLMClient
from bycall.llm.openai_compat import OpenAICompatClient


@pytest.fixture(autouse=True)
def _reset_global_engine():
    """LLM factory 现在读 DB 设置；本文件是纯单元测试，不应受全局 DB 引擎污染。

    清掉 set_engine 设置的全局引擎，让 factory 回退到 config 参数。
    """
    import bycall.db as db_mod

    db_mod.set_engine(None)
    yield
    db_mod.set_engine(None)

# ============ 工具：构造 mock openai client ============


def _mock_chat_response(text: str) -> object:
    """构造 chat.completions.create 的假响应。"""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _mock_embed_response(vectors: list[list[float]]) -> object:
    """构造 embeddings.create 的假响应（带 index 验证对齐逻辑）。"""
    data = [SimpleNamespace(embedding=v, index=i) for i, v in enumerate(vectors)]
    return SimpleNamespace(data=data)


def _client_with_mock(provider: str, **overrides) -> tuple[LLMClient, MagicMock]:
    """按 provider 建客户端，注入 mock SDK client，返回 (client, mock)。"""
    config = LLMConfig(provider=provider, api_key="sk-test", **overrides)
    client = create_llm_client(config)
    mock = MagicMock()
    mock.chat.completions.create.return_value = _mock_chat_response("hello back")
    mock.embeddings.create.return_value = _mock_embed_response([[0.1, 0.2], [0.3, 0.4]])
    # OpenAICompatClient 暴露的测试注入点。
    client._set_client(mock)  # type: ignore[attr-defined]
    return client, mock


# ============ 1. factory 路由 ============


def test_factory_creates_openai_compat():
    client, _ = _client_with_mock("openai")
    assert isinstance(client, OpenAICompatClient)


def test_factory_creates_glm():
    client, _ = _client_with_mock("glm")
    assert isinstance(client, GLMClient)
    # GLMClient 是 OpenAICompatClient 子类（复用兼容协议）。
    assert isinstance(client, OpenAICompatClient)


def test_factory_rejects_unknown_provider():
    # LLMConfig 的 Literal 校验会在构造时就拦下非法 provider，
    # 这里直接测 factory 的防御分支（绕过 config 构造）。
    from bycall.llm.factory import create_llm_client

    fake_config = SimpleNamespace(
        provider="claude", api_key=SimpleNamespace(get_secret_value=lambda: "x")
    )
    with pytest.raises(ValueError, match="不支持的 LLM provider"):
        create_llm_client(fake_config)  # type: ignore[arg-type]


# ============ 2. chat / embed 端到端（mock）============


def test_chat_returns_assistant_text():
    client, mock = _client_with_mock("openai")
    reply = client.chat([Message(role="user", content="hi")])
    assert reply == "hello back"
    mock.chat.completions.create.assert_called_once()
    # 验证消息透传正确
    call_kwargs = mock.chat.completions.create.call_args.kwargs
    assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_empty_choices_returns_empty():
    client, mock = _client_with_mock("openai")
    mock.chat.completions.create.return_value = SimpleNamespace(choices=[])
    assert client.chat([Message(role="user", content="hi")]) == ""


def test_embed_returns_aligned_vectors():
    client, mock = _client_with_mock("openai")
    vecs = client.embed(["hello", "world"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]
    mock.embeddings.create.assert_called_once()


def test_embed_empty_input_returns_empty():
    client, _ = _client_with_mock("openai")
    assert client.embed([]) == []


def test_embed_respects_index_order():
    """embeddings 返回乱序时，按 index 重排（防 SDK 行为漂移）。"""
    client, mock = _client_with_mock("openai")
    # 故意让 index 反序
    mock.embeddings.create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(embedding=[0.9, 0.9], index=1),
            SimpleNamespace(embedding=[0.1, 0.1], index=0),
        ]
    )
    vecs = client.embed(["a", "b"])
    assert vecs == [[0.1, 0.1], [0.9, 0.9]]


# ============ 3. GLM 默认值 ============


def test_glm_default_base_url():
    """GLMClient 不传 base_url 时用智谱默认端点（openai SDK 会规范化加尾斜杠）。"""
    client = GLMClient(api_key="sk-test")
    base = str(client._client.base_url).rstrip("/")  # type: ignore[attr-defined]
    assert base == GLM_DEFAULT_BASE_URL


# ============ 4. async 兜底 ============


@pytest.mark.asyncio
async def test_async_chat_falls_back_to_sync():
    client, _ = _client_with_mock("openai")
    reply = await client.achat([Message(role="user", content="hi")])
    assert reply == "hello back"
