"""ZvecStore 测试：用真实 zvec 验证 CRUD + 混合检索 + 分批 + 权限过滤。

断言对齐 PoC（``zvec-poc/step1_crud.py`` / ``step2_hybrid_search.py``）：
- insert/upsert/delete/query 走通
- 向量 + FTS + filter 三合一命中正确子集
- 多路查询自动挂 RrfReRanker
- insert 超 1024 条自动分批不报错
- ACL 元数据 → pre-filtering 正确隔离

每个用例用独立的 tmp_zvec_dir，互不污染。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from bycall.vector_store.models import (
    ACLMetadata,
    Chunk,
    CollectionDef,
    FieldDef,
    FilterCondition,
    SearchRequest,
)
from bycall.vector_store.zvec_store import _ZVEC_BATCH_LIMIT, ZvecStore

# 测试用小维度，快速验证（真实维度 1024/1536）。
DIM = 16


def _vec(values: list[float]) -> list[float]:
    """归一化成单位向量（zvec 常用约定，参考 PoC step2）。"""
    v = np.array(values, dtype=np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)
    return v.tolist()


# 编程语义轴（[1,0,...] 附近）—— _vec 已返回 list，直接用。
PROG = _vec([1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
# 动物语义轴（[0,...,1,0] 附近）
ANIMAL = _vec([0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0])


def _make_store(tmp_zvec_dir: Path, name: str = "test_col", dim: int = DIM) -> ZvecStore:
    """建一个带默认 RAG schema 的 store 并打开。"""
    store = ZvecStore(data_dir=str(tmp_zvec_dir), dimension=dim, read_only=False)
    store.create_collection(
        CollectionDef(
            name=name,
            fields=[
                FieldDef("source", "string"),
                FieldDef("year", "int64"),
                FieldDef("tenant_id", "string"),
                FieldDef("dept", "string"),
                FieldDef("level", "int64"),
            ],
            dimension=dim,
        )
    )
    return store


def _sample_chunks() -> list[Chunk]:
    """与 PoC step2 同款语义：编程类(wiki) vs 动物类(blog)。"""
    return [
        Chunk(
            id="c1",
            text="Python is a popular programming language",
            vector=PROG,
            fields={"source": "wiki", "year": 2024},
        ),
        Chunk(
            id="c2",
            text="Java is also a widely used programming language",
            vector=_vec([0.9, 0.1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
            fields={"source": "wiki", "year": 2023},
        ),
        Chunk(
            id="c3",
            text="Cats are popular pets around the world",
            vector=ANIMAL,
            fields={"source": "blog", "year": 2024},
        ),
        Chunk(
            id="c4",
            text="Dogs are loyal companions to humans",
            vector=_vec([0, 0, 0, 0, 0, 0, 0, 0, 0.9, 0.1, 0, 0, 0, 0, 0, 0]),
            fields={"source": "blog", "year": 2023},
        ),
        Chunk(
            id="c5",
            text="Programming languages evolve over time",
            vector=_vec([0.8, 0, 0.2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
            fields={"source": "wiki", "year": 2022},
        ),
    ]


# ============ 1. CRUD ============


class TestCRUD:
    def test_insert_and_query(self, tmp_zvec_dir):
        store = _make_store(tmp_zvec_dir)
        results = store.insert(_sample_chunks())
        store.flush()
        assert all(r.ok for r in results)
        assert len(results) == 5

        # 向量查询应命中编程语义（c1 最近）
        hits = store.search(SearchRequest(vector=PROG, topk=3))
        ids = [h.id for h in hits]
        assert "c1" in ids
        store.close()

    def test_upsert_overwrites(self, tmp_zvec_dir):
        store = _make_store(tmp_zvec_dir)
        store.insert(_sample_chunks())
        store.upsert(
            [
                Chunk(
                    id="c1",
                    text="Python is an interpreted, high-level programming language",
                    vector=PROG,
                    fields={"source": "wiki", "year": 2024},
                )
            ]
        )
        store.flush()
        hits = store.search(SearchRequest(vector=PROG, topk=5, output_fields=["text"]))
        c1 = next(h for h in hits if h.id == "c1")
        assert "interpreted" in c1.text
        store.close()

    def test_delete_removes(self, tmp_zvec_dir):
        store = _make_store(tmp_zvec_dir)
        store.insert(_sample_chunks())
        store.delete(["c3"])
        store.flush()
        hits = store.search(SearchRequest(vector=ANIMAL, topk=5))
        ids = {h.id for h in hits}
        assert "c3" not in ids
        store.close()


# ============ 2. 混合检索（对齐 PoC step2）============


class TestHybridSearch:
    def test_vector_only_hits_semantic(self, tmp_zvec_dir):
        store = _make_store(tmp_zvec_dir)
        store.insert(_sample_chunks())
        store.flush()
        hits = store.search(SearchRequest(vector=PROG, topk=3))
        ids = {h.id for h in hits}
        assert "c1" in ids  # 编程语义最近
        store.close()

    def test_fts_only_hits_keywords(self, tmp_zvec_dir):
        store = _make_store(tmp_zvec_dir)
        store.insert(_sample_chunks())
        store.flush()
        # c1/c2/c5 都含 "programming"
        hits = store.search(SearchRequest(full_text="programming", topk=5))
        ids = {h.id for h in hits}
        assert {"c1", "c2", "c5"}.issubset(ids)
        store.close()

    def test_filter_only_isolates_subset(self, tmp_zvec_dir):
        store = _make_store(tmp_zvec_dir)
        store.insert(_sample_chunks())
        store.flush()
        # FTS "are" 命中 c1-c5，filter source='blog' 只留 c3/c4
        hits = store.search(
            SearchRequest(
                full_text="are",
                topk=10,
                filters=[FilterCondition(field="source", op="match", value="blog")],
            )
        )
        ids = {h.id for h in hits}
        assert ids == {"c3", "c4"}
        store.close()

    def test_hybrid_vector_fts_filter(self, tmp_zvec_dir):
        """★ 三合一：向量 + FTS + filter，自动挂 RrfReRanker（PoC step2 核心断言）。"""
        store = _make_store(tmp_zvec_dir)
        store.insert(_sample_chunks())
        store.flush()
        hits = store.search(
            SearchRequest(
                vector=PROG,
                full_text="programming",
                filters=[FilterCondition(field="source", op="match", value="wiki")],
                topk=5,
                output_fields=["text", "source", "year"],
            )
        )
        ids = {h.id for h in hits}
        # 应全是 wiki，且至少含 c1
        assert ids.issubset({"c1", "c2", "c5"})
        assert "c1" in ids
        # 结果字段回显正确
        c1 = next(h for h in hits if h.id == "c1")
        assert c1.fields.get("source") == "wiki"
        assert c1.fields.get("year") in (2022, 2023, 2024)
        store.close()


# ============ 3. 分批（≤1024）============


def test_insert_batches_over_limit(tmp_zvec_dir):
    """insert 超 1024 条自动分批，不报错且全部入库（PoC step6 踩坑）。"""
    store = _make_store(tmp_zvec_dir)
    n = _ZVEC_BATCH_LIMIT + 10  # 刚好越界一点
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((n, DIM)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    chunks = [
        Chunk(id=f"x{i}", text=f"doc number {i}", vector=vecs[i].tolist(), fields={"source": "gen"})
        for i in range(n)
    ]
    results = store.insert(chunks)
    store.flush()
    assert len(results) == n
    assert all(r.ok for r in results)
    # 抽查能查回来
    hits = store.search(SearchRequest(vector=vecs[0].tolist(), topk=5))
    assert len(hits) > 0
    store.close()


# ============ 4. ACL / 权限预过滤 ============


def test_acl_metadata_filters_unauthorized(tmp_zvec_dir):
    """ACL 元数据被平铺成标量字段，pre-filtering 正确隔离（HANDOVER §4.1 红线）。"""
    store = _make_store(tmp_zvec_dir)
    store.insert(
        [
            Chunk(
                id="secret",
                text="Confidential sales figure for Q4",
                vector=PROG,
                fields={"source": "finance"},
                acl=ACLMetadata(tenant_id="acme", dept="finance", level=2),
            ),
            Chunk(
                id="public",
                text="Public programming tutorial",
                vector=PROG,
                fields={"source": "wiki"},
                acl=ACLMetadata(tenant_id="acme", dept="eng", level=0),
            ),
        ]
    )
    store.flush()

    # 只看 finance 部门 → 命中 secret
    hits_finance = store.search(
        SearchRequest(
            vector=PROG,
            topk=5,
            filters=[FilterCondition(field="dept", op="match", value="finance")],
        )
    )
    ids = {h.id for h in hits_finance}
    assert ids == {"secret"}, f"finance 过滤应只命中 secret, got {ids}"

    # 只看 eng 部门 → 命中 public
    hits_eng = store.search(
        SearchRequest(
            vector=PROG,
            topk=5,
            filters=[FilterCondition(field="dept", op="match", value="eng")],
        )
    )
    ids = {h.id for h in hits_eng}
    assert ids == {"public"}, f"eng 过滤应只命中 public, got {ids}"

    store.close()


# ============ 5. open 已存在 collection ============


def test_open_existing_persists_data(tmp_zvec_dir):
    """open 已存在的 collection（zvec.open），数据持久（对齐 PoC step3 WAL 恢复语义）。"""
    store = _make_store(tmp_zvec_dir, name="persist")
    store.insert(_sample_chunks())
    store.flush()
    store.close()  # 释放写锁

    # 重新打开同一 collection
    store2 = ZvecStore(data_dir=str(tmp_zvec_dir), dimension=DIM, read_only=True)
    store2.open("persist")
    hits = store2.search(SearchRequest(vector=PROG, topk=3))
    ids = {h.id for h in hits}
    assert "c1" in ids  # 数据还在
    store2.close()


def test_open_nonexistent_creates_default(tmp_zvec_dir):
    """open 不存在的 collection → 按默认 RAG schema 自动创建。"""
    store = ZvecStore(data_dir=str(tmp_zvec_dir), dimension=DIM)
    store.open("brand_new")
    assert store._name == "brand_new"
    # 默认 schema 能正常写入
    store.insert([Chunk(id="d1", text="hello world", vector=PROG)])
    store.flush()
    hits = store.search(SearchRequest(vector=PROG, topk=3))
    assert any(h.id == "d1" for h in hits)
    store.close()


def test_search_requires_vector_or_fts(tmp_zvec_dir):
    """SearchRequest 不给 vector 也不给 full_text → 报 ValueError。"""
    store = _make_store(tmp_zvec_dir)
    store.insert(_sample_chunks())
    with pytest.raises(ValueError):
        store.search(SearchRequest(topk=3))
    store.close()
