"""LLM 抽象测试：拆分后的 ChatModel / EmbedModel 端到端 + resolver + factory。

覆盖：
- ``OpenAIChatModel.chat`` 走通（mock SDK 响应）
- ``OpenAIEmbedModel.embed`` 返回对齐的向量 + ``dimension`` property
- GLM 兼容层（``GLMClient`` = ``OpenAIChatModel`` + 智谱默认 URL）
- ``LLMClient`` 复合接口的 async 兜底（chat 路径）
- ``resolver``：DB 激活 ``Model`` 实体 → ChatModel / EmbedModel
- ``factory``：``create_chat_model`` / ``create_embed_model`` / ``create_llm_client``

所有 LLM 调用用 mock SDK client，不连真实 API。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from sqlmodel import Session, create_engine

from bycall.config import LLMConfig
from bycall.llm.base import ChatModel, EmbedModel, LLMClient, Message
from bycall.llm.factory import create_chat_model, create_embed_model, create_llm_client
from bycall.llm.glm import GLM_DEFAULT_BASE_URL, GLMClient
from bycall.llm.openai_compat import OpenAIChatModel, OpenAIEmbedModel


@pytest.fixture(autouse=True)
def _reset_global_engine():
    """factory/resolver 现在读 DB；本文件纯单元测试，不应受全局 DB 引擎污染。

    清掉 set_engine 设置的全局引擎，让 factory 的 config 兜底路径可用。
    """
    import bycall.db as db_mod

    db_mod.set_engine(None)
    yield
    db_mod.set_engine(None)


@pytest.fixture(autouse=True)
def _fake_openai_api_key(monkeypatch):
    """OpenAI SDK 构造时若 key 为空会报错；测试统一灌一个假 key，避免真连网。

    DB 驱动测试会真实构造 ``OpenAIChatModel`` / ``OpenAIEmbedModel``（不注入 mock），
    需要绕过 SDK 的凭据校验。不设环境变量则构造阶段即抛 OpenAIError。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    yield


# ============ 工具：构造 mock openai client ============


def _mock_chat_response(text: str) -> object:
    """构造 chat.completions.create 的假响应。"""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _mock_embed_response(vectors: list[list[float]]) -> object:
    """构造 embeddings.create 的假响应（带 index 验证对齐逻辑）。"""
    data = [SimpleNamespace(embedding=v, index=i) for i, v in enumerate(vectors)]
    return SimpleNamespace(data=data)


def _make_chat_model(model: str = "gpt-4o-mini") -> tuple[OpenAIChatModel, MagicMock]:
    """构造 OpenAIChatModel 并注入 mock SDK client，返回 (model, mock)。"""
    chat = OpenAIChatModel(
        base_url="https://api.openai.com/v1", api_key="sk-test", model=model, timeout=60.0
    )
    mock = MagicMock()
    mock.chat.completions.create.return_value = _mock_chat_response("hello back")
    chat._set_client(mock)  # type: ignore[attr-defined]
    return chat, mock


def _make_embed_model(
    model: str = "text-embedding-3-small", dimension: int = 8
) -> tuple[OpenAIEmbedModel, MagicMock]:
    """构造 OpenAIEmbedModel 并注入 mock SDK client，返回 (model, mock)。"""
    embed = OpenAIEmbedModel(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        model=model,
        dimension=dimension,
        timeout=60.0,
    )
    mock = MagicMock()
    mock.embeddings.create.return_value = _mock_embed_response([[0.1, 0.2], [0.3, 0.4]])
    embed._set_client(mock)  # type: ignore[attr-defined]
    return embed, mock


def _kwargs_model(mock_method: MagicMock) -> str:
    """取 mock 被调用时传的 model 参数（kwargs 优先，兼容 positional）。"""
    call = mock_method.call_args
    if call is None:
        return ""
    return call.kwargs.get("model") or ""


# ============ 1. ChatModel.chat 端到端（mock）============


def test_chat_returns_assistant_text():
    chat, mock = _make_chat_model()
    reply = chat.chat([Message(role="user", content="hi")])
    assert reply == "hello back"
    mock.chat.completions.create.assert_called_once()
    # 验证消息透传正确
    call_kwargs = mock.chat.completions.create.call_args.kwargs
    assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert call_kwargs["model"] == "gpt-4o-mini"


def test_chat_empty_choices_returns_empty():
    chat, mock = _make_chat_model()
    mock.chat.completions.create.return_value = SimpleNamespace(choices=[])
    assert chat.chat([Message(role="user", content="hi")]) == ""


def test_chat_model_is_chat_model():
    chat, _ = _make_chat_model()
    assert isinstance(chat, ChatModel)
    # OpenAIChatModel 只承担对话，不应是 EmbedModel。
    assert not isinstance(chat, EmbedModel)


# ============ 2. EmbedModel.embed 端到端（mock）============


def test_embed_returns_aligned_vectors():
    embed, mock = _make_embed_model(dimension=2)
    vecs = embed.embed(["hello", "world"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]
    mock.embeddings.create.assert_called_once()
    assert _kwargs_model(mock.embeddings.create) == "text-embedding-3-small"


def test_embed_empty_input_returns_empty():
    embed, _ = _make_embed_model()
    assert embed.embed([]) == []


def test_embed_respects_index_order():
    """embeddings 返回乱序时，按 index 重排（防 SDK 行为漂移）。"""
    embed, mock = _make_embed_model()
    # 故意让 index 反序
    mock.embeddings.create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(embedding=[0.9, 0.9], index=1),
            SimpleNamespace(embedding=[0.1, 0.1], index=0),
        ]
    )
    vecs = embed.embed(["a", "b"])
    assert vecs == [[0.1, 0.1], [0.9, 0.9]]


def test_embed_dimension_property():
    embed, _ = _make_embed_model(dimension=1536)
    assert embed.dimension == 1536
    embed2, _ = _make_embed_model(dimension=768)
    assert embed2.dimension == 768


def test_embed_model_is_embed_model():
    embed, _ = _make_embed_model()
    assert isinstance(embed, EmbedModel)
    # OpenAIEmbedModel 只承担向量化，不应是 ChatModel。
    assert not isinstance(embed, ChatModel)


# ============ 3. GLM 兼容层 ============


def test_glm_default_base_url():
    """GLMClient 不传 base_url 时用智谱默认端点（openai SDK 会规范化加尾斜杠）。"""
    client = GLMClient(api_key="sk-test")
    base = str(client._client.base_url).rstrip("/")  # type: ignore[attr-defined]
    assert base == GLM_DEFAULT_BASE_URL


def test_glm_client_is_openai_chat_model():
    """GLMClient 复用 OpenAIChatModel 兼容协议，且只承担对话。"""
    client = GLMClient(api_key="sk-test")
    assert isinstance(client, OpenAIChatModel)
    assert isinstance(client, ChatModel)
    assert not isinstance(client, EmbedModel)


# ============ 4. LLMClient 复合接口 + async 兜底 ============


def test_llm_client_composite_iface():
    """LLMClient 是 ChatModel + EmbedModel 复合体，mock spec 能同时拿到 chat/embed。"""
    m = MagicMock(spec=LLMClient)
    assert hasattr(m, "chat")
    assert hasattr(m, "embed")
    assert hasattr(m, "dimension")


@pytest.mark.asyncio
async def test_async_chat_falls_back_to_sync():
    chat, _ = _make_chat_model()
    reply = await chat.achat([Message(role="user", content="hi")])
    assert reply == "hello back"


@pytest.mark.asyncio
async def test_async_embed_falls_back_to_sync():
    embed, _ = _make_embed_model(dimension=2)
    vecs = await embed.aembed(["hello", "world"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


# ============ 5. resolver + factory（DB 驱动）============


def _new_session(tmp_path: Path) -> Session:
    """建一个独立的内存 SQLite Session，含 Model 表，供 resolver/factory 测试。"""
    from sqlmodel import SQLModel

    import bycall.db as db_mod
    import bycall.governance.models  # noqa: F401  注册 SQLModel 表到 metadata

    engine = create_engine(
        f"sqlite:///{tmp_path}/resolver.db", connect_args={"check_same_thread": False}
    )
    db_mod.set_engine(engine)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_models(session: Session) -> None:
    """灌一条激活 chat 模型 + 一条激活 embedding 模型。"""
    from bycall.governance.models import Model

    session.add(
        Model(
            name="glm-4-flash",
            display_name="智谱 GLM-4-Flash",
            model_type="chat",
            source="remote",
            provider="zhipu",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="sk-zhipu",
            is_active=True,
        )
    )
    session.add(
        Model(
            name="bge-m3",
            display_name="本地 BGE-M3",
            model_type="embedding",
            source="local",
            provider="ollama",
            base_url="http://localhost:11434/v1",
            # 本地 Ollama 本不需要 key，但 OpenAI SDK 不允许空 api_key 构造，
            # 这里给一个占位值（与真实调用无关，测试只验证 base_url / dimension）。
            api_key="ollama-no-key",
            embed_dimension=1024,
            is_active=True,
        )
    )
    session.commit()


def test_create_chat_model_from_db(tmp_path):
    """create_chat_model 从 DB 激活 chat 实体构造 ChatOpenAI（智谱 endpoint）。"""
    session = _new_session(tmp_path)
    _seed_models(session)
    chat = create_chat_model(session)
    assert chat is not None
    # M3：对话模型统一切到 langchain-openai 的 ChatOpenAI（原生 bind_tools）。
    assert isinstance(chat, ChatOpenAI)
    assert isinstance(chat, BaseChatModel)
    # 实例应从 Model 实体取 base_url。
    assert str(chat.openai_api_base).rstrip("/") == "https://open.bigmodel.cn/api/paas/v4"


def test_create_embed_model_from_db(tmp_path):
    """create_embed_model 从 DB 激活 embedding 实体构造 OpenAIEmbedModel（本地维度）。"""
    session = _new_session(tmp_path)
    _seed_models(session)
    embed = create_embed_model(session)
    assert embed is not None
    assert isinstance(embed, OpenAIEmbedModel)
    assert embed.dimension == 1024
    # embedding 用的是本地 ollama endpoint，与 chat 的智谱 endpoint 不同（解耦验证）。
    assert str(embed._client.base_url).rstrip("/") == "http://localhost:11434/v1"  # type: ignore[attr-defined]


def test_create_model_returns_none_when_no_active(tmp_path):
    """无激活模型时 create_chat_model / create_embed_model 返回 None。"""
    session = _new_session(tmp_path)
    assert create_chat_model(session) is None
    assert create_embed_model(session) is None


def test_resolver_get_chat_model_pair(tmp_path):
    """resolver.get_chat_model 返回 (entity, model) 元组。"""
    from bycall.llm.resolver import get_chat_model

    session = _new_session(tmp_path)
    _seed_models(session)
    pair = get_chat_model(session)
    assert pair is not None
    entity, model = pair
    assert entity.name == "glm-4-flash"
    # M3：model 现在是 ChatOpenAI（BaseChatModel），不再是自研 ChatModel。
    assert isinstance(model, BaseChatModel)


# ============ 6. factory 兼容入口（config 兜底）============


def test_create_llm_client_config_fallback():
    """无 session 时，create_llm_client 走 config 兜底，返回 ChatOpenAI。"""
    config = LLMConfig(provider="openai", api_key="sk-test", model="gpt-4o-mini")
    client = create_llm_client(config)
    # M3：兜底路径也构造 ChatOpenAI。
    assert isinstance(client, ChatOpenAI)
    assert isinstance(client, BaseChatModel)


def test_create_llm_client_prefers_db_when_session(tmp_path):
    """有 session 且 DB 有激活 chat 模型时，create_llm_client 走 DB 而非 config。"""
    session = _new_session(tmp_path)
    _seed_models(session)
    client = create_llm_client(config=None, session=session)
    assert isinstance(client, ChatOpenAI)
    # DB 里是智谱 endpoint，证明走的是 DB 路径而非 config 默认 openai。
    assert str(client.openai_api_base).rstrip("/") == "https://open.bigmodel.cn/api/paas/v4"


def test_create_llm_client_config_fallback_when_no_active(tmp_path):
    """有 session 但 DB 无激活模型时，create_llm_client 回退到 config 构造。"""
    session = _new_session(tmp_path)
    config = LLMConfig(provider="openai", api_key="sk-test", model="gpt-4o-mini")
    client = create_llm_client(config, session=session)
    assert isinstance(client, ChatOpenAI)
    # config 默认是 OpenAI 官方 endpoint（非 DB 的智谱）。
    assert str(client.openai_api_base).rstrip("/") == "https://api.openai.com/v1"
