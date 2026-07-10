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
from bycall.rag.chunking import Document, documents_to_pre_chunks, split_document
from bycall.rag.ingest import IngestPipeline
from bycall.rag.retrieve import build_rag_messages, retrieve_context
from bycall.vector_store.factory import create_vector_store

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
        chunks = split_document(doc, max_chunk_size=500, overlap=50)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 500

    def test_multi_paragraph_split(self):
        """多段落在段落边界切开，不合并。"""
        text = "段落一内容。\n\n段落二内容。\n\n段落三内容。"
        doc = Document(doc_id="d3", text=text, source="public")
        chunks = split_document(doc)
        assert len(chunks) == 3
        assert "段落一" in chunks[0]
        assert "段落二" in chunks[1]
        assert "段落三" in chunks[2]

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
        # docA 切成 2 块，docB 切成 1 块。
        assert len(pre) == 3
        assert pre[0].id == "docA_0"
        assert pre[1].id == "docA_1"
        assert pre[2].id == "docB_0"
        # source 正确
        assert pre[0].source == "sales"
        assert pre[1].source == "sales"
        assert pre[2].source == "hr"
        # doc_id 正确
        assert pre[0].doc_id == "docA"
        assert pre[2].doc_id == "docB"

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
        """消息结构：system prompt + context system + user question。"""
        from bycall.vector_store.models import SearchResult

        retrieved = [
            SearchResult(id="r1", score=1.0, text="知识片段 A", fields={"source": "sales"}),
            SearchResult(id="r2", score=0.8, text="知识片段 B", fields={"source": "hr"}),
        ]
        messages = build_rag_messages(
            system_prompt="你是数字员工。",
            question="报价流程是什么？",
            retrieved=retrieved,
        )
        assert len(messages) == 3
        # 第 1 条：控制面 system prompt
        assert messages[0].role == "system"
        assert messages[0].content == "你是数字员工。"
        # 第 2 条：知识库上下文（system，含来源与文本）
        assert messages[1].role == "system"
        assert "[sales]" in messages[1].content
        assert "知识片段 A" in messages[1].content
        assert "[hr]" in messages[1].content
        assert "知识片段 B" in messages[1].content
        # 第 3 条：用户问题
        assert messages[2].role == "user"
        assert messages[2].content == "报价流程是什么？"

    def test_empty_retrieved_still_has_context_msg(self):
        """无检索结果时第二条 system 仍存在（内容空）。"""
        messages = build_rag_messages(system_prompt="你是数字员工。", question="问题", retrieved=[])
        assert len(messages) == 3
        assert messages[1].role == "system"
        assert "知识库" in messages[1].content
