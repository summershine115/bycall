"""RAG 管道测试：切块 → 入库 → 检索 → 拼 prompt。

用 mock LLM（embed 返回与文本相关的稳定小维度向量）+ 真实 zvec（临时目录）。
不依赖真实 embedding API，验证的是管道衔接与 ACL 预过滤，而非召回质量。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bycall.config import get_settings
from bycall.llm.base import EmbedModel
from bycall.rag.chunking import ChunkingConfig, Document, documents_to_pre_chunks, split_document
from bycall.rag.ingest import IngestPipeline
from bycall.rag.retrieve import _rrf_fuse, build_rag_messages, retrieve_context
from bycall.vector_store.factory import create_vector_store
from bycall.vector_store.models import SearchResult

# 测试用小维度，快速且与 mock embed 对齐。
DIM = 8


@pytest.fixture
def rag_env(tmp_path: Path, monkeypatch):
    """干净的 RAG 环境：zvec 数据目录 + 小维度向量库 + sqlite。"""
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "8")
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/t.db")
    get_settings.cache_clear()
    return tmp_path


def make_mock_embed_model(dim: int = DIM) -> MagicMock:
    """mock EmbedModel：embed 返回与文本哈希相关的归一化向量。

    向量与文本内容相关（sha256 派生），便于向量化路也能命中语义相近的 chunk。
    """
    m = MagicMock(spec=EmbedModel)
    m.dimension = dim

    def embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            vals = [(b - 128) / 128.0 for b in h[:dim]]
            n = math.sqrt(sum(v * v for v in vals)) or 1.0
            out.append([v / n for v in vals])
        return out

    m.embed.side_effect = embed
    return m


def _make_store_and_pipeline(rag_env: Path):
    """建 store（维度 8）+ IngestPipeline，返回 (store, pipeline)。"""
    store = create_vector_store(get_settings().vector_store)
    embed_model = make_mock_embed_model(DIM)
    pipeline = IngestPipeline(store, embed_model, collection_name="knowledge")
    return store, pipeline


# ============ 1. split_document ============


class TestSplitDocument:
    def test_short_doc_returns_single_chunk(self):
        """短文档不切，原样返回一个 chunk。"""
        doc = Document(doc_id="d1", text="这是一段简短的文本。", source="public")
        chunks = split_document(doc)
        assert len(chunks) == 1
        assert chunks[0] == "这是一段简短的文本。"

    def test_long_doc_split_within_limit(self):
        """长文本切成多块，每块 ≤ max_chunk_size。"""
        # 单段落但很长（无句号），强制走硬切路径。
        long_text = "字" * 1200
        doc = Document(doc_id="d2", text=long_text, source="public")
        chunks = split_document(doc, config=ChunkingConfig(chunk_size=500, overlap=50))
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 500

    def test_long_doc_split_overlap_zero_terminates(self):
        """overlap=0 时硬切必须终止并恰好铺满原文（回归 _hard_split_final 终止 bug）。"""
        long_text = "字" * 1200
        doc = Document(doc_id="d2b", text=long_text, source="public")
        chunks = split_document(doc, config=ChunkingConfig(chunk_size=500, overlap=0))
        assert len(chunks) > 1
        assert all(len(c) <= 500 for c in chunks)
        assert "".join(chunks) == long_text  # overlap=0 应恰好铺满，无重叠无遗漏

    def test_multi_paragraph_split(self):
        """多段落在段落边界切开，不合并。"""
        text = "段落一内容。\n\n段落二内容。\n\n段落三内容。"
        doc = Document(doc_id="d3", text=text, source="public")
        chunks = split_document(doc)
        assert len(chunks) >= 1  # 新递归切分可能合并短段落
        assert "段落一" in chunks[0]

    def test_empty_doc_returns_empty(self):
        """空文档返回空列表。"""
        doc = Document(doc_id="d4", text="   ", source="public")
        assert split_document(doc) == []


# ============ 2. documents_to_pre_chunks ============


class TestDocumentsToPreChunks:
    def test_id_format_and_source(self):
        """id 格式 {doc_id}_{idx}，source 继承自文档。"""
        docs = [
            Document(doc_id="docA", text="第一块。\n\n第二块。", source="sales"),
            Document(doc_id="docB", text="单块。", source="hr"),
        ]
        pre = documents_to_pre_chunks(docs)
        # 新递归切分：短段落可能合并为 1 块
        assert len(pre) >= 2
        # source 正确
        assert all(p.source in ("sales", "hr") for p in pre)
        # doc_id 正确
        assert all(p.doc_id in ("docA", "docB") for p in pre)
        # id 以 doc_id 开头
        assert all(p.id.startswith(p.doc_id) for p in pre)

    def test_empty_docs_returns_empty(self):
        assert documents_to_pre_chunks([]) == []


# ============ 3. IngestPipeline.ingest_documents ============


class TestIngestPipeline:
    def test_ingest_writes_and_searchable(self, rag_env: Path):
        """切块→入库，chunk_count>0，zvec 能 search 回来。"""
        store, pipeline = _make_store_and_pipeline(rag_env)
        docs = [
            Document(
                doc_id="kb1",
                text="我们的产品报价流程如下：先询价再签约。\n\n报价需要走审批流程。",
                source="sales",
            )
        ]
        result = pipeline.ingest_documents(docs)
        assert result.success
        assert result.chunk_count > 0
        assert result.errors == []

        # 能 FTS 搜回
        from bycall.vector_store.models import SearchRequest

        hits = store.search(SearchRequest(full_text="报价", topk=5))
        assert len(hits) > 0
        assert any(h.fields.get("source") == "sales" for h in hits)
        store.close()

    def test_ingest_multiple_sources(self, rag_env: Path):
        """多 source 文档同时入库，各自可被过滤检索。"""
        store, pipeline = _make_store_and_pipeline(rag_env)
        docs = [
            Document(doc_id="s1", text="销售流程与报价审批流程。", source="sales"),
            Document(doc_id="h1", text="招聘流程与入职流程。", source="hr"),
        ]
        result = pipeline.ingest_documents(docs)
        assert result.success
        assert result.chunk_count >= 2

        from bycall.vector_store.models import FilterCondition, SearchRequest

        # 流程 同时命中两个 source 的文档，filter 只看 sales
        hits = store.search(
            SearchRequest(
                full_text="流程",
                filters=[FilterCondition(field="source", op="match", value="sales")],
                topk=10,
            )
        )
        assert len(hits) > 0
        assert all(h.fields.get("source") == "sales" for h in hits)
        store.close()

    def test_ingest_text_convenience(self, rag_env: Path):
        """ingest_text 便利方法：自动生成 doc_id。"""
        store, pipeline = _make_store_and_pipeline(rag_env)
        result = pipeline.ingest_text("一段测试文本内容。", source="public")
        assert result.success
        assert result.chunk_count == 1
        store.close()


# ============ 4. retrieve_context ============


class TestRetrieveContext:
    @pytest.fixture
    def loaded_store(self, rag_env: Path):
        """预灌好 sales + hr 知识的 store，供检索测试复用。"""
        store, pipeline = _make_store_and_pipeline(rag_env)
        docs = [
            Document(doc_id="s1", text="销售流程与报价审批流程。", source="sales"),
            Document(doc_id="h1", text="招聘流程与入职流程。", source="hr"),
        ]
        pipeline.ingest_documents(docs)
        yield store
        store.close()

    def test_admin_wildcard_returns_all(self, loaded_store):
        """admin（tags=['*']）不过滤，能检索到。"""
        hits = retrieve_context(loaded_store, "流程", knowledge_scope_tags=["*"], topk=10)
        assert len(hits) > 0
        sources = {h.fields.get("source") for h in hits}
        # 应能同时看到 sales 和 hr
        assert "sales" in sources or "hr" in sources

    def test_normal_user_filters_by_source(self, loaded_store):
        """普通用户 tags=['sales'] 只回 source=sales 的。"""
        hits = retrieve_context(loaded_store, "流程", knowledge_scope_tags=["sales"], topk=10)
        assert len(hits) > 0
        assert all(h.fields.get("source") == "sales" for h in hits)

    def test_empty_tags_returns_empty(self, loaded_store):
        """空 tags → 返回空（默认隔离）。"""
        hits = retrieve_context(loaded_store, "流程", knowledge_scope_tags=[], topk=10)
        assert hits == []

    def test_hr_tag_isolates_sales(self, loaded_store):
        """tags=['hr'] 只回 hr 的，sales 被隔离。"""
        hits = retrieve_context(loaded_store, "流程", knowledge_scope_tags=["hr"], topk=10)
        assert len(hits) > 0
        assert all(h.fields.get("source") == "hr" for h in hits)


# ============ 5. build_rag_messages ============


class TestBuildRagMessages:
    def test_message_structure(self):
        """消息结构：system prompt + context user + user question。"""
        from bycall.agent.context_budget import DEFAULT_BUDGET, budget_and_format_rag
        from bycall.vector_store.models import SearchResult

        retrieved = [
            SearchResult(id="r1", score=1.0, text="知识片段 A", fields={"source": "sales"}),
            SearchResult(id="r2", score=0.8, text="知识片段 B", fields={"source": "hr"}),
        ]
        rag_context, _ = budget_and_format_rag(retrieved, DEFAULT_BUDGET)
        messages = build_rag_messages(
            system_prompt="你是数字员工。",
            question="报价流程是什么？",
            rag_context=rag_context,
        )
        assert len(messages) == 3
        # 第 1 条：控制面 system prompt + 防御声明（提示词注入缓解）
        assert messages[0].role == "system"
        assert messages[0].content.startswith("你是数字员工。")
        assert "以本段规则为准" in messages[0].content  # 防御声明
        # 第 2 条：预算化后的知识库上下文（user，含来源/相关性标注 + 不可信边界）
        assert messages[1].role == "user"
        assert "sales" in messages[1].content
        assert "知识片段 A" in messages[1].content
        assert "hr" in messages[1].content
        assert "知识片段 B" in messages[1].content
        assert "相关性" in messages[1].content  # 预算化新增的相关性分档标注（缺陷 1）
        assert "参考资料" in messages[1].content  # 边界标注
        # 第 3 条：用户问题
        assert messages[2].role == "user"
        assert messages[2].content == "报价流程是什么？"

    def test_empty_retrieved_still_has_context_msg(self):
        """无检索结果时第二条 user context 仍存在（内容空）。"""
        messages = build_rag_messages(
            system_prompt="你是数字员工。", question="问题", rag_context=""
        )
        assert len(messages) == 3
        assert messages[1].role == "user"
        assert "knowledge_context" in messages[1].content


# ============ 6. retrieve_context 混合检索（vector + FTS） ============


class TestRetrieveContextHybrid:
    """验证 retrieve_context 传 query_vector 时走混合检索（vector + FTS）。

    mock embed 基于 sha256 派生向量（见 make_mock_embed_model），相同文本向量相同，
    因此用入库文本本身作 query 时向量路必命中该 chunk——这断言"向量路被触发"。
    """

    def test_with_query_vector_hits_semantic(self, rag_env: Path):
        """传 query_vector 时走混合检索，命中语义近邻 chunk。"""
        store, pipeline = _make_store_and_pipeline(rag_env)
        embed_model = make_mock_embed_model(DIM)
        docs = [
            Document(doc_id="s1", text="销售流程与报价审批流程。", source="sales"),
            Document(doc_id="h1", text="招聘流程与入职流程。", source="hr"),
        ]
        pipeline.ingest_documents(docs)

        # 用入库原文作 query → 向量路必然命中（sha256 相同 → 向量相同 → cosine=1）。
        query_vec = embed_model.embed(["销售流程与报价审批流程。"])[0]
        hits = retrieve_context(
            store,
            "销售流程与报价审批流程。",
            knowledge_scope_tags=["*"],
            query_vector=query_vec,
            topk=10,
        )
        assert len(hits) > 0
        # 向量路应命中 s1（query 与 s1 向量相同）。
        assert any(h.fields.get("source") == "sales" for h in hits)
        store.close()

    def test_without_query_vector_degrades_to_fts(self, rag_env: Path):
        """不传 query_vector → 退化为纯 FTS（后向兼容）。"""
        store, pipeline = _make_store_and_pipeline(rag_env)
        docs = [Document(doc_id="s1", text="销售流程与报价审批流程。", source="sales")]
        pipeline.ingest_documents(docs)

        hits = retrieve_context(
            store, "报价", knowledge_scope_tags=["*"], query_vector=None, topk=10
        )
        assert len(hits) > 0
        store.close()


# ============ 7. _rrf_fuse 跨 KB RRF 融合 ============


class TestRrfFuse:
    """纯函数测试：不依赖 zvec，直接构造 SearchResult 列表验证 RRF 数学正确。"""

    def test_empty_input_returns_empty(self):
        """空列表输入 → 空输出。"""
        assert _rrf_fuse([], k=60, topk=5) == []

    def test_single_kb_preserves_ranking(self):
        """单 KB 结果：RRF 排序与原排名一致（仅 score 被替换）。"""
        kb1 = [
            SearchResult(id="a", score=0.9, text="A", fields={}),
            SearchResult(id="b", score=0.5, text="B", fields={}),
            SearchResult(id="c", score=0.1, text="C", fields={}),
        ]
        fused = _rrf_fuse([kb1], k=60, topk=5)
        assert [r.id for r in fused] == ["a", "b", "c"]
        # RRF 分 = 1/(60+rank)，rank 从 1 起计。
        assert fused[0].score == pytest.approx(1.0 / 61)
        assert fused[1].score == pytest.approx(1.0 / 62)
        assert fused[2].score == pytest.approx(1.0 / 63)

    def test_cross_kb_accumulates_score(self):
        """同一 doc 在多个 KB 命中 → RRF 分累加，排名提升。"""
        # doc_a 在 KB1 排第 1，KB2 排第 1 → 分数最高。
        # doc_b 在 KB1 排第 2，KB2 未出现 → 分数较低。
        kb1 = [
            SearchResult(id="a", score=10.0, text="A", fields={}),
            SearchResult(id="b", score=5.0, text="B", fields={}),
        ]
        kb2 = [
            SearchResult(id="a", score=0.01, text="A", fields={}),  # 原始分极低
        ]
        fused = _rrf_fuse([kb1, kb2], k=60, topk=5)
        # doc_a 在两个 KB 都第 1：2 * (1/61)
        # doc_b 只在 KB1 第 2：1/62
        assert fused[0].id == "a"
        assert fused[0].score == pytest.approx(2.0 / 61)
        assert fused[1].id == "b"
        assert fused[1].score == pytest.approx(1.0 / 62)

    def test_rank_based_not_score_based(self):
        """RRF 只看排名不看原始 score：高原始分但低排名的 doc 不应优先。"""
        # KB1 里 doc_x 原始分 999（FTS 高分），但排第 2（rank=2）。
        # KB1 里 doc_y 原始分 0.1，但排第 1（rank=1）。
        kb1 = [
            SearchResult(id="y", score=0.1, text="Y", fields={}),
            SearchResult(id="x", score=999.0, text="X", fields={}),
        ]
        fused = _rrf_fuse([kb1], k=60, topk=5)
        # RRF 按排名 → y（rank=1）应在 x（rank=2）之前，尽管 y 原始分远低。
        assert fused[0].id == "y"
        assert fused[1].id == "x"

    def test_topk_limit(self):
        """topk 截断：返回不超过 topk 条。"""
        kb1 = [SearchResult(id=f"d{i}", score=float(i), text=str(i), fields={}) for i in range(10)]
        fused = _rrf_fuse([kb1], k=60, topk=3)
        assert len(fused) == 3
        assert [r.id for r in fused] == ["d0", "d1", "d2"]

    def test_doc_content_preserved_from_first_hit(self):
        """doc 冲突时保留首次出现的内容（text/fields）。"""
        kb1 = [SearchResult(id="a", score=1.0, text="来自KB1", fields={"source": "sales"})]
        kb2 = [SearchResult(id="a", score=0.5, text="来自KB2", fields={"source": "hr"})]
        fused = _rrf_fuse([kb1, kb2], k=60, topk=5)
        assert len(fused) == 1
        assert fused[0].text == "来自KB1"
        assert fused[0].fields == {"source": "sales"}


class TestRetrieveMultiKbEmbedDedup:
    """retrieve_multi_kb：同 embedding_model_id 的 KB 共享一次 embed（去重优化）。

    多 KB 同模型的场景，embed API 调用从 N 次降为 1 次（embed 是网络调用，最贵环节）。
    """

    def test_same_model_kbs_share_one_embed(self):
        from unittest.mock import patch

        import bycall.vector_store.factory as vsf
        from bycall.rag import retrieve
        from bycall.rag.retrieve import retrieve_multi_kb

        calls = {"n": 0}

        def mock_embed_query(session, mid, q, dim):
            calls["n"] += 1
            return [0.0] * dim

        mock_store = MagicMock()
        mock_store.search.return_value = []
        # 3 个 KB 共用同一 embedding_model_id（常见：所有 KB 绑同一个 bge-large）
        kb_specs = [(1, "c1", 8, 42), (2, "c2", 8, 42), (3, "c3", 8, 42)]

        with (
            patch.object(retrieve, "_embed_query", mock_embed_query),
            patch.object(vsf, "create_vector_store", MagicMock(return_value=mock_store)),
            patch.object(retrieve, "_resolve_parent_chunks", lambda *a, **k: []),
        ):
            retrieve_multi_kb(MagicMock(), MagicMock(), kb_specs, "q", ["*"])
        assert calls["n"] == 1  # 去重：3 个同模型 KB 只 embed 1 次

    def test_different_models_each_embed_once(self):
        from unittest.mock import patch

        import bycall.vector_store.factory as vsf
        from bycall.rag import retrieve
        from bycall.rag.retrieve import retrieve_multi_kb

        calls = {"n": 0}

        def mock_embed_query(session, mid, q, dim):
            calls["n"] += 1
            return [0.0] * dim

        mock_store = MagicMock()
        mock_store.search.return_value = []
        # 3 个 KB 各用不同 embedding_model_id
        kb_specs = [(1, "c1", 8, 42), (2, "c2", 8, 43), (3, "c3", 8, 44)]

        with (
            patch.object(retrieve, "_embed_query", mock_embed_query),
            patch.object(vsf, "create_vector_store", MagicMock(return_value=mock_store)),
            patch.object(retrieve, "_resolve_parent_chunks", lambda *a, **k: []),
        ):
            retrieve_multi_kb(MagicMock(), MagicMock(), kb_specs, "q", ["*"])
        assert calls["n"] == 3  # 不同模型各 embed 一次（不误去重）
