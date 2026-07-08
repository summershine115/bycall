"""VectorStore 抽象 + zvec 实现。

后端无关接口（``VectorStore``）在 ``base.py``，共享数据模型在 ``models.py``，
zvec 实现在 ``zvec_store.py``，工厂在 ``factory.py``。

迁移到 pgvector/Qdrant 时：新增一个实现类，在 ``factory.py`` 注册即可，
业务代码无需改动。详见 ``docs/decisions/0001-vectorstore-abstraction.md``。
"""

from bycall.vector_store.base import VectorStore
from bycall.vector_store.models import (
    ACLMetadata,
    Chunk,
    CollectionDef,
    FieldDef,
    InsertResult,
    SearchRequest,
    SearchResult,
)

__all__ = [
    "ACLMetadata",
    "Chunk",
    "CollectionDef",
    "FieldDef",
    "InsertResult",
    "SearchRequest",
    "SearchResult",
    "VectorStore",
]
