"""zvec 后端实现。

把 PoC（``zvec-poc/step1~6``）验证过的 API 用法封装成 ``VectorStore`` 接口。
所有 zvec 特有的坑都封在这里，业务层只见干净接口。

⚠️ zvec 硬约束（来自 PoC 踩坑，写死在实现里）：
  1. insert/upsert 单批 ≤1024 条，超出报错 → ``_batched()`` 自动分批。
  2. filter 语法用单等号 ``=``（不是 ``==``）→ ``_build_filter()``。
  3. 多路查询（vector + fts 同时给）必须指定 ``reranker`` → search 里自动挂
     ``zvec.RrfReRanker()``（倒数排名融合，不依赖外部模型）。
  4. 打开已存在的库用 ``zvec.open(path)``（不是 create_and_open）→ ``open()`` 探测。
  5. Collection 无 ``close()`` 方法 → ``close()`` 用 ``del`` + ``gc.collect()``
     释放写锁（多进程并发读场景的关键，见 PoC step6）。
  6. 多进程并发读必须 ``CollectionOption(read_only=1)`` → 配置项 ``read_only`` 暴露。

并发模型（HANDOVER §3.2）：本场景 <50 并发、查为主偶尔写，单进程 + 多线程读即可，
无需多进程读写分离。``read_only`` 默认 False（单进程读写）。
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Iterator
from pathlib import Path

import zvec
from zvec import (
    CollectionSchema,
    DataType,
    Doc,
    FieldSchema,
    Fts,
    FtsIndexParam,
    HnswIndexParam,
    HnswQueryParam,
    Query,
    VectorSchema,
)
from zvec.model.param import CollectionOption

from bycall.vector_store.base import VectorStore
from bycall.vector_store.models import (
    Chunk,
    CollectionDef,
    FieldDef,
    FilterCondition,
    InsertResult,
    SearchRequest,
    SearchResult,
)

logger = logging.getLogger(__name__)

# zvec insert 单批硬上限（PoC step6 踩坑）。
_ZVEC_BATCH_LIMIT = 1024

# 后端无关 FieldDef.type → zvec DataType 映射。
_TYPE_MAP: dict[str, DataType] = {
    "string": DataType.STRING,
    "int64": DataType.INT64,
    "float": DataType.FLOAT,
    "bool": DataType.BOOL,
}


class ZvecStore(VectorStore):
    """zvec 后端。一个实例 = 一个 collection（目录级隔离天然支持多租户边界）。"""

    def __init__(self, data_dir: str, dimension: int, read_only: bool = False) -> None:
        self._data_dir = Path(data_dir)
        self._dimension = dimension
        self._read_only = read_only
        self._collection: zvec.Collection | None = None
        self._name: str | None = None

    # ---------- 生命周期 ----------

    def create_collection(self, schema: CollectionDef) -> None:
        """创建并打开新 collection。目录已存在则抛错（防误覆盖）。"""
        path = self._collection_path(schema.name)
        if path.exists() and any(path.iterdir()):
            raise FileExistsError(f"collection 目录已存在且非空: {path}")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        zvec_schema = self._build_schema(schema)
        self._collection = zvec.create_and_open(path=str(path), schema=zvec_schema)
        self._name = schema.name
        logger.info("created zvec collection %s @ %s", schema.name, path)

    def open(self, name: str) -> None:
        """打开已存在的 collection；不存在则按默认 schema 创建（RAG 知识库通用形态）。"""
        path = self._collection_path(name)
        option = CollectionOption(read_only=1) if self._read_only else None
        if path.exists() and any(path.iterdir()):
            self._collection = (
                zvec.open(path=str(path), option=option)
                if option is not None
                else zvec.open(path=str(path))
            )
            logger.info("opened existing zvec collection %s", name)
        else:
            # 不存在 → 用默认 RAG schema 创建（正文 FTS + 默认 ACL 标量字段）。
            logger.info("collection %s 不存在，按默认 RAG schema 创建", name)
            self.create_collection(self._default_rag_schema(name, self._dimension))
        self._name = name

    def close(self) -> None:
        """zvec Collection 无 close()，靠 GC 释放写锁（PoC step6）。

        主动 ``del`` + ``gc.collect()`` 确保写锁及时释放，便于多进程后续只读打开。
        只读模式跳过 flush（只读 collection 不允许 Flush）。
        """
        if self._collection is not None:
            if not self._read_only:
                try:
                    self._collection.flush()
                except Exception as e:
                    logger.warning("close 时 flush 失败（忽略，继续释放）: %s", e)
            del self._collection
            self._collection = None
            gc.collect()
            logger.info("closed zvec collection %s (via gc)", self._name)

    def destroy(self, name: str) -> None:
        """永久删除 collection（zvec Collection.destroy() + GC）。

        删 KB 时用。先 close 当前打开的（如果是同一个），再 destroy。
        zvec 的 destroy 会删掉磁盘上的 collection 目录。
        """
        path = self._collection_path(name)
        if not path.exists():
            logger.info("collection %s 不存在，跳过 destroy", name)
            return

        # 如果当前正打开的是这个 collection，先关掉
        if self._name == name:
            self.close()

        # 重新打开后 destroy
        option = CollectionOption(read_only=0)
        col = zvec.open(path=str(path), option=option)
        col.destroy()
        gc.collect()
        logger.info("destroyed zvec collection %s @ %s", name, path)

    def describe(self, name: str) -> dict:
        """查 collection 信息（维度 / 文档数），不存在返回空 dict。

        不改变当前打开的 collection——用临时 open + close 查信息。
        注意 zvec 的 ``schema`` / ``stats`` 是属性不是方法（Pybind11 def_property）。
        """
        path = self._collection_path(name)
        if not path.exists() or not any(path.iterdir()):
            return {}
        try:
            option = CollectionOption(read_only=1)
            col = zvec.open(path=str(path), option=option)
            schema = col.schema
            stats = col.stats
            info = {
                "name": name,
                "dimension": getattr(schema.vectors[0], "dimension", 0) if schema.vectors else 0,
                "doc_count": getattr(stats, "doc_count", 0),
            }
            del col
            gc.collect()
            return info
        except Exception as e:
            logger.warning("describe collection %s 失败: %s", name, e)
            return {}

    # ---------- 写 ----------

    def insert(self, docs: list[Chunk]) -> list[InsertResult]:
        return self._write(docs, mode="insert")

    def upsert(self, docs: list[Chunk]) -> list[InsertResult]:
        return self._write(docs, mode="upsert")

    def delete(self, ids: list[str]) -> None:
        self._require_open().delete(ids)

    def delete_by_filter(self, conditions: list[FilterCondition]) -> int:
        """按过滤条件批量删除（zvec ``delete_by_filter``）。

        用于级联删除：删某文档的所有 chunk（``doc_id = xxx``）。
        条件翻译成 zvec 单等号 filter 字符串（复用 ``_build_filter``）。
        """
        filter_str = self._build_filter(conditions)
        if not filter_str:
            return 0
        self._require_open().delete_by_filter(filter_str)
        return 0  # zvec delete_by_filter 不返回删除条数

    def flush(self) -> None:
        self._require_open().flush()

    # ---------- 检索 ----------

    def search(self, query: SearchRequest) -> list[SearchResult]:
        """混合检索：vector / full_text / filters 任意组合。

        - 仅 vector：单路向量查询。
        - 仅 full_text：单路 FTS 查询。
        - 两者都有：多路查询，自动挂 ``RrfReRanker``（PoC step2/step5）。
        - filters 始终作为召回前过滤（pre-filtering），翻译成 zvec 单等号字符串。
        """
        col = self._require_open()
        zvec_queries = self._build_queries(query)
        if not zvec_queries:
            raise ValueError("SearchRequest 至少要给 vector 或 full_text 之一")

        kwargs: dict[str, object] = {
            "queries": zvec_queries,
            "topk": query.topk,
            "output_fields": query.output_fields,
        }
        filter_str = self._build_filter(query.filters)
        if filter_str:
            kwargs["filter"] = filter_str
        # 多路必须 reranker（zvec 0.5+ 强制）。
        if len(zvec_queries) > 1:
            kwargs["reranker"] = zvec.RrfReRanker()

        raw = col.query(**kwargs)
        return [self._to_result(d) for d in raw]

    # ---------- 内部：路径 / schema ----------

    def _collection_path(self, name: str) -> Path:
        return self._data_dir / name

    @staticmethod
    def _build_schema(defn: CollectionDef) -> CollectionSchema:
        fields: list[FieldSchema] = []
        # 正文字段：FTS 索引（混合检索的关键）。
        fields.append(
            FieldSchema(
                "text",
                DataType.STRING,
                nullable=False,
                index_param=FtsIndexParam(tokenizer_name="standard"),
            )
        )
        # 用户自定义标量字段（排除会与内置冲突的 text/vector_field）。
        reserved = {"text", defn.vector_field}
        for f in defn.fields:
            if f.name in reserved:
                raise ValueError(f"字段名 {f.name!r} 与内置字段冲突")
            index_param = (
                FtsIndexParam(tokenizer_name="standard") if f.fts and f.type == "string" else None
            )
            fields.append(
                FieldSchema(f.name, _TYPE_MAP[f.type], nullable=f.nullable, index_param=index_param)
            )
        vectors = [
            VectorSchema(
                defn.vector_field,
                DataType.VECTOR_FP32,
                dimension=defn.dimension,
                index_param=HnswIndexParam(),
            )
        ]
        return CollectionSchema(name=defn.name, fields=fields, vectors=vectors)

    @staticmethod
    def _default_rag_schema(name: str, dimension: int) -> CollectionDef:
        """通用 RAG schema：正文(FTS) + source + doc_id + 默认 ACL 标量字段。

        - ``source``：知识域 ACL 标签，检索时翻译成 ``source IN (...)`` 预过滤（主权限门）。
        - ``doc_id``：文档 id，按文档级联删除时用（zvec 无前缀删，靠标量 filter 查出再删）。
        - ``tenant_id``/``dept``/``level``：M2 ABAC 预过滤用，M1 先建好索引。

        ``dimension`` 由 store 配置传入，须与 embedding 模型对齐。
        """
        return CollectionDef(
            name=name,
            dimension=dimension,
            fields=[
                FieldDef("source", "string"),
                FieldDef("doc_id", "string"),
                FieldDef("tenant_id", "string"),
                FieldDef("dept", "string"),
                FieldDef("level", "int64"),
            ],
        )

    # ---------- 内部：写 ----------

    def _write(self, docs: list[Chunk], mode: str) -> list[InsertResult]:
        col = self._require_open()
        results: list[InsertResult] = []
        op = getattr(col, mode)  # insert 或 upsert
        for batch in self._batched(docs, _ZVEC_BATCH_LIMIT):
            zdocs = [self._chunk_to_doc(c) for c in batch]
            raw = op(zdocs)
            for doc_id, r in zip([c.id for c in batch], raw, strict=False):
                results.append(
                    InsertResult(id=doc_id, ok=bool(r.ok()), message=str(getattr(r, "message", "")))
                )
        return results

    @staticmethod
    def _batched(docs: list[Chunk], size: int) -> Iterator[list[Chunk]]:
        """zvec 单批 ≤1024（PoC step6），自动分批。"""
        for i in range(0, len(docs), size):
            yield docs[i : i + size]

    def _chunk_to_doc(self, chunk: Chunk) -> Doc:
        """把后端无关的 Chunk → zvec Doc。ACL 元数据平铺成标量字段。"""
        fields: dict[str, object] = {"text": chunk.text, **chunk.fields}
        if chunk.acl is not None:
            fields["tenant_id"] = chunk.acl.tenant_id
            if chunk.acl.dept is not None:
                fields["dept"] = chunk.acl.dept
            if chunk.acl.level is not None:
                fields["level"] = chunk.acl.level
        return Doc(id=chunk.id, fields=fields, vectors={"embedding": chunk.vector})

    # ---------- 内部：检索 ----------

    def _build_queries(self, query: SearchRequest) -> list[Query]:
        """构造 zvec 多路查询列表。至少一路；vector+fts 同在则两路。"""
        queries: list[Query] = []
        if query.vector is not None:
            queries.append(
                Query(field_name="embedding", vector=query.vector, param=HnswQueryParam(ef=64))
            )
        if query.full_text:
            queries.append(Query(field_name="text", fts=Fts(match_string=query.full_text)))
        return queries

    @staticmethod
    def _build_filter(filters: list[FilterCondition]) -> str:
        """结构化条件 → zvec 单等号 filter 字符串。

        zvec 用单等号 ``=``（不是 ``==``），多条件 AND 拼接（PoC step2 踩坑）。
        in_ 用 ``field IN ('a','b')`` 语法（zvec 支持），值用单引号包裹字符串。
        """
        parts: list[str] = []
        for cond in filters:
            if cond.op == "match":
                val = _quote_value(cond.value)
                parts.append(f"{cond.field} = {val}")
            elif cond.op == "in":
                vals = ", ".join(_quote_value(v) for v in (cond.values or []))
                parts.append(f"{cond.field} IN ({vals})")
        return " AND ".join(parts)

    @staticmethod
    def _to_result(d: object) -> SearchResult:
        """zvec 结果 doc → 后端无关 SearchResult。"""
        fields = dict(getattr(d, "fields", {}) or {})
        text = str(fields.pop("text", ""))
        return SearchResult(
            id=getattr(d, "id", ""),
            score=float(getattr(d, "score", 0.0)),
            text=text,
            fields=fields,
        )

    # ---------- 内部：守卫 ----------

    def _require_open(self) -> zvec.Collection:
        if self._collection is None:
            raise RuntimeError(
                f"collection 未打开，先调 open()/create_collection() (name={self._name})"
            )
        return self._collection


def _quote_value(v: object) -> str:
    """zvec filter 里字符串值要单引号包裹，数值不裹。"""
    if isinstance(v, str):
        escaped = v.replace("'", "\\'")
        return f"'{escaped}'"
    return str(v)


__all__ = ["ZvecStore"]
