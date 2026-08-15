"""评测用例公共设施：隔离的临时 DB / zvec / 环境变量。

评测用例**不碰** ``bycall.db`` 的全局引擎（避免与运行中的服务 / pytest 夹具互踩），
每个需要 DB 的用例都开自己的内存 SQLite；需要向量检索的用例用真实 zvec + 临时目录。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from bycall.config import get_settings


@contextmanager
def isolated_settings(**env: str) -> Iterator[None]:
    """临时覆盖进程环境变量并清 settings 缓存，退出时恢复。

    zvec 用例用 ``BYCALL_VECTOR_STORE_DATA_DIR`` / ``BYCALL_VECTOR_STORE_DIMENSION``
    指向临时目录，保证离线、可重复、互不干扰。
    """
    saved = {key: os.environ.get(key) for key in env}
    for key, value in env.items():
        os.environ[key] = value
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@contextmanager
def eval_session() -> Iterator[Session]:
    """一个隔离的 SQLite 内存 Session（表结构全量建好，数据由用例自备）。"""
    from bycall.governance import models as _models  # noqa: F401  注册全部表元数据

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@contextmanager
def eval_zvec(tmp_dir: Path, dimension: int = 8):
    """一个隔离的真实 zvec store（已 open，用例退出时 close）。

    zvec 是嵌入式的——零外部依赖，符合「评测默认离线可跑」的约束。
    """
    from bycall.vector_store.factory import create_vector_store

    data_dir = tmp_dir / "zvec"
    data_dir.mkdir(parents=True, exist_ok=True)
    with isolated_settings(
        BYCALL_VECTOR_STORE_DATA_DIR=str(data_dir),
        BYCALL_VECTOR_STORE_DIMENSION=str(dimension),
        BYCALL_VECTOR_STORE_BACKEND="zvec",
    ):
        store = create_vector_store(get_settings().vector_store, dimension=dimension)
        try:
            yield store
        finally:
            store.close()


def zero_embed_model(dimension: int = 8):
    """零向量 embedding 桩——zvec 检索场景不依赖真实 LLM。"""
    from unittest.mock import MagicMock

    from bycall.llm.base import EmbedModel

    model = MagicMock(spec=EmbedModel)
    model.dimension = dimension
    model.embed.side_effect = lambda texts: [[0.0] * dimension for _ in texts]
    return model


def ensure_builtin_tools() -> None:
    """确保内置工具（calculator 等）已注册。

    pytest 的 conftest 为隔离会 ``clear_registry()``，而 ``bycall.tools`` 包只会
    import 时注册一次——包已在 sys.modules 后不会重复执行注册。依赖内置工具的评测
    用例在运行前调用本函数，用 reload 幂等重放注册（register 重名覆盖，无副作用）。
    """
    import importlib

    import bycall.tools.builtins as builtins

    importlib.reload(builtins)


def assert_true(condition: bool, message: str, **evidence) -> None:
    """用例内断言助手：失败时带证据抛 ``EvalFailure``。"""
    if not condition:
        from bycall.evals.core import EvalFailure

        raise EvalFailure(message, dict(evidence))
