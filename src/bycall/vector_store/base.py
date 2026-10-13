"""VectorStore 后端无关抽象接口。

设计目标（见 ``doc/decisions/0001-vectorstore-abstraction.md``）：
1. 对齐迁移目标 —— 方法签名与 Qdrant/pgvector 概念对齐，换后端不改业务代码。
2. 权限预过滤一等公民 —— ``search(filters=...)`` 支持结构化 ACL 条件（M2 真正用）。
3. 混合检索 —— 单次 ``search`` 可同时给 vector + full_text + filters。

各后端实现：
- zvec: ``zvec_store.ZvecStore``（M0）
- pgvector / qdrant: 迁移时新增实现，在 ``factory.py`` 注册。

实现类应做"艰苦的适配"：把后端特有的坑（zvec 的单批≤1024、单等号 filter、
无 close()、多进程读需 read_only 等）封在自己内部，对外只暴露这套干净接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bycall.vector_store.models import (
    Chunk,
    CollectionDef,
    FilterCondition,
    InsertResult,
    SearchRequest,
    SearchResult,
)


class VectorStore(ABC):
    """向量库后端抽象。一个实例绑定一个 collection。"""

    @abstractmethod
    def create_collection(self, schema: CollectionDef) -> None:
        """创建并打开一个新 collection。已存在则报错（语义对齐 zvec create_and_open）。"""

    @abstractmethod
    def open(self, name: str) -> None:
        """打开已存在的 collection。实现需自动探测 path/库是否存在再决定 open/create。"""

    @abstractmethod
    def insert(self, docs: list[Chunk]) -> list[InsertResult]:
        """插入。实现须自动分批（zvec 单批 ≤1024）。id 冲突行为由后端定。"""

    @abstractmethod
    def upsert(self, docs: list[Chunk]) -> list[InsertResult]:
        """按 id 覆盖写。"""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """按 id 删除。"""

    @abstractmethod
    def delete_by_filter(self, conditions: list[FilterCondition]) -> int:
        """按过滤条件批量删除（zvec delete_by_filter）。返回删除条数（尽力统计，可能为 0）。

        用于级联删除：删某文档的所有 chunk（doc_id = xxx）。
        后端不支持时 raise NotImplementedError，调用方回退到 query+delete。
        """

    def get(
        self,
        ids: list[str],
        filters: list[FilterCondition] | None = None,
        output_fields: list[str] | None = None,
    ) -> list[SearchResult]:
        """精确读取 ID；正文返回前执行 filters，后端未实现时拒绝扩展。"""
        raise NotImplementedError("后端未实现带授权检查的精确读取")

    @abstractmethod
    def search(self, query: SearchRequest) -> list[SearchResult]:
        """混合检索。vector / full_text / filters 任意组合；多路时后端自动 rerank。"""

    @abstractmethod
    def flush(self) -> None:
        """强制落盘（WAL → segment）。"""

    @abstractmethod
    def close(self) -> None:
        """释放底层资源（zvec 无 close()，实现靠 del + gc；其他后端走原生 close）。"""

    @abstractmethod
    def destroy(self, name: str) -> None:
        """永久删除 collection（删 KB 时用）。不可恢复。

        与 close() 的区别：close 只释放连接，destroy 删掉磁盘数据。
        """

    @abstractmethod
    def describe(self, name: str) -> dict:
        """查 collection 信息（维度 / 文档数 / 大小），不存在返回空 dict。"""
