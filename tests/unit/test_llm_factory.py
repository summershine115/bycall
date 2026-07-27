"""LLM 抽象测试：EmbedModel 端到端 + resolver + factory。

覆盖：
- ``OpenAIEmbedModel.embed`` 返回对齐的向量 + ``dimension`` property
- ``EmbedModel.aembed`` async 兜底
- ``resolver``：DB 激活 ``Model`` 实体 → ChatOpenAI / EmbedModel
- ``factory``：``create_chat_model`` / ``create_embed_model``

对话模型（M3）已切到 ``langchain-openai`` 的 ``ChatOpenAI``，自研 chat 抽象与 GLM
兼容层已移除（见 ADR-0002），不再测试。

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

from bycall.llm.base import EmbedModel
from bycall.llm.factory import create_chat_model, create_embed_model
from bycall.llm.openai_compat import OpenAIEmbedModel


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

    DB 驱动测试会真实构造 ``OpenAIEmbedModel``（不注入 mock），需要绕过 SDK 的凭据
    校验。不设环境变量则构造阶段即抛 OpenAIError。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    yield


# ============ 工具：构造 mock openai client ============


def _mock_embed_response(vectors: list[list[float]]) -> object:
    """构造 embeddings.create 的假响应（带 index 验证对齐逻辑）。"""
    data = [SimpleNamespace(embedding=v, index=i) for i, v in enumerate(vectors)]
    return SimpleNamespace(data=data)


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


# ============ 1. EmbedModel.embed 端到端（mock）============


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


# ============ 2. EmbedModel async 兜底 ============


@pytest.mark.asyncio
async def test_async_embed_falls_back_to_sync():
    embed, _ = _make_embed_model(dimension=2)
    vecs = await embed.aembed(["hello", "world"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]


# ============ 3. resolver + factory（DB 驱动）============


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
    # M5 后 resolver 包 _MeteredEmbedModel（metric wrapper），顶层断言接口、内部断言具体实现。
    assert isinstance(embed, EmbedModel)
    assert isinstance(embed._inner, OpenAIEmbedModel)
    assert embed.dimension == 1024
    # embedding 用的是本地 ollama endpoint，与 chat 的智谱 endpoint 不同（解耦验证）。
    assert str(embed._inner._client.base_url).rstrip("/") == "http://localhost:11434/v1"  # type: ignore[attr-defined]


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
    # M3：model 是 ChatOpenAI（BaseChatModel）。
    assert isinstance(model, BaseChatModel)
