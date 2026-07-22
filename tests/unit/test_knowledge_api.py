"""知识库文档管理 Admin REST API 单元测试（per-KB 版）。

覆盖：
- 创建文档并入库（source ACL 标注，绑定到 KB）
- 列表 + source 过滤
- 文档详情（status=completed, chunk_count>0, embedding_model_id 记录）
- 无绑定 embedding 模型 → 400 + status=failed
- 级联删除（删文档 → zvec chunks 清理）
- reindex（重新入库）
- sources 列表
- 文件上传（multipart）
- 更新文档标题（不触发重入库）
- 更新 source（触发重入库）

每个测试先建 embedding Model 记录 + KnowledgeBase，再在 KB 下建文档。
``resolve_embed_model`` 被 mock（_ingest_document 内部调用），返回 sha256 派生的稳定小维度向量。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.llm import resolver
from bycall.llm.base import EmbedModel

DIM = 16


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """挂载 admin router 的子 app + 全新 SQLite + mock embedding。

    ``resolve_embed_model`` 被 patch 成返回 mock EmbedModel（sha256 派生向量）。
    这样入库流程不需要真实 API key 也不连真实 embedding 服务。
    """
    db_path = tmp_path / "test_knowledge.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", str(DIM))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    # mock embedding：embed 返回 sha256 派生的归一化向量
    mock_embed = MagicMock(spec=EmbedModel)
    mock_embed.dimension = DIM

    def embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            vals = [(b - 128) / 128.0 for b in h[:DIM]]
            n = math.sqrt(sum(v * v for v in vals)) or 1.0
            out.append([v / n for v in vals])
        return out

    mock_embed.embed.side_effect = embed
    monkeypatch.setattr(resolver, "resolve_embed_model", lambda session, model: mock_embed)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _create_embedding_model(client: TestClient, dim: int = DIM, name: str = "mock-embed") -> int:
    """建一个 embedding 类型 Model 记录（KB 创建需要），返回 id。"""
    resp = client.post(
        "/api/admin/models",
        json={
            "name": name,
            "model_type": "embedding",
            "embed_dimension": dim,
            "is_active": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_kb(client: TestClient, embed_model_id: int, name: str = "测试知识库") -> int:
    """建一个 KnowledgeBase，返回 id。"""
    resp = client.post(
        "/api/admin/knowledge-bases",
        json={
            "name": name,
            "description": "",
            "embedding_model_id": embed_model_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_doc(
    client: TestClient,
    kb_id: int,
    *,
    title: str = "销售流程手册",
    source: str = "sales",
    content: str = "我们的报价流程如下：先询价再签约。\n\n报价需要走审批流程。",
):
    """在指定 KB 下建文档并入库。"""
    return client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents",
        json={
            "knowledge_base_id": kb_id,
            "title": title,
            "source": source,
            "content": content,
        },
    )


# ============ 创建 + 入库 ============


def test_create_document_ingests_successfully(client: TestClient):
    """创建文档 → 201 + ok=True + chunk_count>0 + status=completed。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = _create_doc(client, kb_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["chunk_count"] > 0
    assert body["errors"] == []

    # 文档详情确认状态
    doc_id = body["document_id"]
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["status"] == "completed"
    assert detail["chunk_count"] > 0
    assert detail["source"] == "sales"
    assert detail["embedding_model_id"] == mid


def test_create_empty_content_returns_400(client: TestClient):
    """空正文 → 400。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = _create_doc(client, kb_id, content="   ")
    assert resp.status_code == 400


def test_create_empty_source_returns_400(client: TestClient):
    """空 source → 400。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = _create_doc(client, kb_id, source="  ")
    assert resp.status_code == 400


# ============ 列表 + 过滤 ============


def test_list_documents_and_filter_by_source(client: TestClient):
    """多文档不同 source，列表全返回，source 过滤生效。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    _create_doc(client, kb_id, title="销售文档", source="sales", content="销售内容。")
    _create_doc(client, kb_id, title="HR文档", source="hr", content="HR内容。")

    # 全部
    all_docs = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents").json()
    assert len(all_docs) == 2

    # 按 source 过滤
    sales = client.get(
        f"/api/admin/knowledge-bases/{kb_id}/documents", params={"source": "sales"}
    ).json()
    assert len(sales) == 1
    assert sales[0]["source"] == "sales"


def test_list_sources(client: TestClient):
    """sources 端点返回去重排序的 source 列表。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    _create_doc(client, kb_id, title="A", source="hr", content="内容")
    _create_doc(client, kb_id, title="B", source="sales", content="内容")
    _create_doc(client, kb_id, title="C", source="sales", content="内容2")

    sources = client.get("/api/admin/knowledge/sources").json()
    assert sources == ["hr", "sales"]  # 去重 + 排序


# ============ 级联删除 ============


def _get_zvec_collection_doc_count(kb_id: int) -> int:
    """直接读 zvec collection 的 stats.doc_count。

    用于验证删除/reindex 后向量库里的文档数变化。注意 zvec 关闭重开后 FTS
    索引可能未即时生效，所以这里用 stats（物理计数）而非 FTS 搜索。
    """
    from sqlmodel import Session, create_engine

    from bycall.config import get_settings
    from bycall.governance.models import KnowledgeBase
    from bycall.vector_store.factory import create_vector_store

    settings = get_settings()
    engine = create_engine(settings.postgres.dsn, connect_args={"check_same_thread": False})
    with Session(engine) as s:
        kb = s.get(KnowledgeBase, kb_id)
        assert kb is not None, f"KB {kb_id} 不存在"

    info = create_vector_store(settings.vector_store, dimension=kb.dimension).describe(
        kb.collection_name
    )
    return info.get("doc_count", 0)


def test_delete_document_cleans_chunks(client: TestClient):
    """删文档 → 204 + zvec 文档数减少 + DB 记录删除。

    强化原测试：不只验证 DB 记录删除，还查 zvec stats 确认 chunk 真的删掉了。
    （之前 doc_id 过滤前缀不匹配导致 delete_by_filter 命中 0 条，旧 chunk 残留。）
    """
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    create_resp = _create_doc(
        client, kb_id, title="待删", source="public", content="要被删除的唯一内容。"
    )
    doc_id = create_resp.json()["document_id"]
    chunk_count = create_resp.json()["chunk_count"]
    assert chunk_count > 0

    # 删除前：zvec 里有 chunk
    before = _get_zvec_collection_doc_count(kb_id)
    assert before >= chunk_count, f"删除前 zvec 应有 ≥{chunk_count} 个文档，实际 {before}"

    # 删除
    del_resp = client.delete(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}")
    assert del_resp.status_code == 204

    # DB 记录已删
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}")
    assert detail.status_code == 404

    # 列表里也没了
    listing = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents").json()
    assert all(d["id"] != doc_id for d in listing)

    # 关键：zvec 文档数应减少 chunk_count（之前 delete_by_filter 命中 0 条，不减）
    after = _get_zvec_collection_doc_count(kb_id)
    assert after == before - chunk_count, (
        f"删除后 zvec 文档数应减少 {chunk_count}（before={before} after={after}）"
    )


def test_delete_nonexistent_returns_404(client: TestClient):
    """删不存在的文档 → 404。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.delete(f"/api/admin/knowledge-bases/{kb_id}/documents/9999")
    assert resp.status_code == 404


# ============ reindex ============


def test_reindex_document(client: TestClient):
    """reindex → 重新入库，chunk_count 与之前一致（同 content 同 mock embed）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    create_resp = _create_doc(
        client, kb_id, title="重入库", source="public", content="要重新入库的内容。"
    )
    doc_id = create_resp.json()["document_id"]
    original_count = create_resp.json()["chunk_count"]

    reindex_resp = client.post(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reindex")
    assert reindex_resp.status_code == 200
    assert reindex_resp.json()["ok"] is True
    assert reindex_resp.json()["chunk_count"] == original_count


def test_reindex_does_not_leave_ghost_chunks(client: TestClient):
    """reindex 后 chunk 数不翻倍：upsert 覆盖写 + 删旧 filter 正确。

    回归幽灵 chunk bug：之前 delete 用 ``str(doc_id)`` 而入库用 ``doc_{doc_id}``，
    reindex 的"删旧"步骤删不掉旧 chunk；加上 ingest 用 ``insert``（遇到 ALREADY_EXISTS
    静默失败但报 success=True），导致旧内容残留、新内容没写入。
    现在改用 upsert + 正确 filter，reindex 后文档数应不变（覆盖写）。
    """
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    create_resp = _create_doc(
        client, kb_id, title="重入库", source="public", content="唯一的重入库内容。"
    )
    doc_id = create_resp.json()["document_id"]
    original_count = create_resp.json()["chunk_count"]
    before = _get_zvec_collection_doc_count(kb_id)
    assert before == original_count, f"初始文档数应为 {original_count}，实际 {before}"

    # reindex
    reindex_resp = client.post(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reindex")
    assert reindex_resp.status_code == 200
    assert reindex_resp.json()["ok"] is True

    # 关键：reindex 后 chunk 数应等于 original_count（upsert 覆盖，不是 insert 追加）
    assert reindex_resp.json()["chunk_count"] == original_count
    after = _get_zvec_collection_doc_count(kb_id)
    assert after == original_count, (
        f"reindex 后文档数应不变（={original_count}），实际 {after}（幽灵 chunk 累积）"
    )


# ============ 更新 ============


def test_update_title_no_reingest(client: TestClient):
    """改标题不触发重入库（source 不变）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    create_resp = _create_doc(client, kb_id, title="原标题", source="sales", content="内容。")
    doc_id = create_resp.json()["document_id"]

    resp = client.put(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}",
        json={"title": "新标题"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "新标题"
    assert resp.json()["source"] == "sales"


def test_update_source_triggers_reingest(client: TestClient):
    """改 source 触发重入库。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    create_resp = _create_doc(client, kb_id, title="标题", source="sales", content="内容。")
    doc_id = create_resp.json()["document_id"]

    resp = client.put(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}",
        json={"source": "hr"},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "hr"


# ============ 文件上传 ============


def test_upload_txt_file(client: TestClient):
    """上传 .txt 文件 → 201 + 入库成功。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public", "title": "上传的文档"},
        files={"file": ("test.txt", "这是上传的文本内容。\n\n第二段。".encode(), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["chunk_count"] > 0

    # 文档元数据正确
    detail = client.get(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{body['document_id']}"
    ).json()
    assert detail["file_name"] == "test.txt"
    assert detail["file_type"] == "txt"


def test_upload_unsupported_format_returns_400(client: TestClient):
    """上传不支持的格式 → 400。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public"},
        files={"file": ("doc.pdf", b"fake pdf", "application/pdf")},
    )
    assert resp.status_code == 400


def test_upload_non_utf8_returns_400(client: TestClient):
    """上传非 UTF-8 二进制 → 400。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public"},
        files={"file": ("bad.txt", b"\xff\xfe\x00\x01", "text/plain")},
    )
    assert resp.status_code == 400


def test_delete_document_keeps_metadata_when_vector_delete_fails(client: TestClient, monkeypatch):
    """向量删除失败必须 fail-closed，不能制造仍可检索的幽灵 chunk。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    created = _create_doc(client, kb_id, title="保留元数据", source="public", content="内容")
    doc_id = created.json()["document_id"]

    import bycall.admin.api as admin_api

    monkeypatch.setattr(
        admin_api,
        "_delete_chunks_for_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("zvec unavailable")),
    )
    response = client.delete(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}")
    assert response.status_code == 503
    assert client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").status_code == 200


def test_source_change_rolls_back_when_vector_delete_fails(client: TestClient, monkeypatch):
    """ACL 更新失败时 DB source 必须回滚到旧授权状态。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    created = _create_doc(client, kb_id, title="ACL 文档", source="public", content="内容")
    doc_id = created.json()["document_id"]

    import bycall.admin.api as admin_api

    monkeypatch.setattr(
        admin_api,
        "_delete_chunks_for_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("zvec unavailable")),
    )
    response = client.put(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}",
        json={"source": "hr"},
    )
    assert response.status_code == 503
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["source"] == "public"


# ============ 文档详情 404 ============


def test_get_nonexistent_returns_404(client: TestClient):
    """查不存在的文档 → 404。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/9999")
    assert resp.status_code == 404


# ============ 切块预览（参考 WeKnora chunker/preview）===========


def test_preview_chunks_default(client: TestClient):
    """切块预览：不 embedding、不落库，返回切块明细。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    # 多段长文本（> chunk_size）→ 多块
    text = "产品介绍。\n\n" + ("这是一段较长的产品功能描述，用于测试切块边界是否生效。\n\n" * 30)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents/preview-chunks",
        json={"text": text, "title": "预览文档"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] > 1
    assert body["total"] == len(body["items"])
    assert body["child_count"] == body["total"]  # 默认非父子块，全是 child
    assert body["parent_count"] == 0
    assert all("text" in i and "char_count" in i for i in body["items"])
    # 预览不落库：KB 切块计数不变
    kb = client.get(f"/api/admin/knowledge-bases/{kb_id}").json()
    assert kb["chunk_count"] == 0
    assert kb["document_count"] == 0


def test_preview_chunks_parent_child(client: TestClient):
    """开启父子块 → 产出 child + parent 两类。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    text = "标题。\n\n" + ("段落内容用于测试父子块切分。\n\n" * 40)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents/preview-chunks",
        json={"text": text, "enable_parent_child": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parent_count"] > 0
    assert body["child_count"] > 0
    assert body["total"] == body["parent_count"] + body["child_count"]


def test_preview_chunks_kb_not_found(client: TestClient):
    """预览不存在的 KB → 404。"""
    resp = client.post(
        "/api/admin/knowledge-bases/9999/documents/preview-chunks",
        json={"text": "任意文本"},
    )
    assert resp.status_code == 404


# ============ 文档正文读取 ============


def test_get_document_content(client: TestClient):
    """读取文档原始正文（DocumentOut 不带 content）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    body = _create_doc(client, kb_id, content="这是用于核对入库的正文内容。").json()
    doc_id = body["document_id"]
    resp = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/content")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["content"] == "这是用于核对入库的正文内容。"
    assert data["id"] == doc_id


def test_get_document_content_cross_kb_404(client: TestClient):
    """文档不属于该 KB → 404。"""
    mid = _create_embedding_model(client)
    kb_a = _create_kb(client, mid, name="知识库A")
    kb_b = _create_kb(client, mid, name="知识库B")
    doc_id = _create_doc(client, kb_a).json()["document_id"]
    resp = client.get(f"/api/admin/knowledge-bases/{kb_b}/documents/{doc_id}/content")
    assert resp.status_code == 404


# ============ 入库失败终态：status=failed + error_message（修复"失败无日志"）===========


def test_ingest_failure_records_error_message(client: TestClient, monkeypatch):
    """embedding 失败 → 文档 status=failed 且 error_message 非空，不卡在 pending/processing。

    对齐 WeKnora dead-letter：入库失败必须进入终态并记录原因。
    """
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)

    # 让 embedding 抛异常（模拟 embedding 服务不可用）
    failing = MagicMock(spec=EmbedModel)
    failing.dimension = DIM
    failing.embed.side_effect = RuntimeError("embedding 服务连接超时")
    monkeypatch.setattr(resolver, "resolve_embed_model", lambda session, model: failing)

    resp = _create_doc(client, kb_id, content="一段需要入库但 embedding 会失败的正文。")
    assert resp.status_code == 201  # 文档已创建
    assert resp.json()["ok"] is False  # 入库失败

    detail = client.get(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{resp.json()['document_id']}"
    ).json()
    assert detail["status"] == "failed"
    assert "embedding" in detail["error_message"]


def test_upload_failure_records_error_message(client: TestClient, monkeypatch):
    """文件上传入库失败 → status=failed + error_message（同步化前的 NameError 会卡死）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)

    failing = MagicMock(spec=EmbedModel)
    failing.dimension = DIM
    failing.embed.side_effect = RuntimeError("embedding 服务 500")
    monkeypatch.setattr(resolver, "resolve_embed_model", lambda session, model: failing)

    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public", "title": "上传会失败的文档"},
        files={
            "file": (
                "fail.txt",
                "这是一段会因为 embedding 失败而入库失败的文本。".encode(),
                "text/plain",
            )
        },
    )
    assert resp.status_code == 201
    assert resp.json()["ok"] is False
    doc_id = resp.json()["document_id"]
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_message"]
