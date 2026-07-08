"""VectorStore 工厂：按配置实例化后端实现。

新增后端（pgvector/qdrant）：在此注册一个分支，实现 ``VectorStore`` 接口即可。
"""

from __future__ import annotations

from bycall.config import VectorStoreConfig
from bycall.vector_store.base import VectorStore


def create_vector_store(config: VectorStoreConfig) -> VectorStore:
    """按 ``config.backend`` 实例化一个未打开的 VectorStore。

    调用方负责后续 ``open(name)`` 或 ``create_collection(schema)``。
    """
    backend = config.backend
    if backend == "zvec":
        # 局部 import，避免未装 zvec 时整个模块不可用（迁移后端时可能不装 zvec）。
        from bycall.vector_store.zvec_store import ZvecStore

        return ZvecStore(
            data_dir=config.data_dir,
            dimension=config.dimension,
            read_only=config.read_only,
        )
    if backend in ("pgvector", "qdrant"):
        raise NotImplementedError(
            f"backend={backend!r} 计划在迁移阶段（M5）实现；当前 M0 仅支持 zvec。"
            " 触发迁移条件时见 docs/decisions/0001-vectorstore-abstraction.md。"
        )
    raise ValueError(f"不支持的 vector store backend: {backend!r}")


__all__ = ["create_vector_store"]
