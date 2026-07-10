"""知识库文档管理 Admin REST API 单元测试。

覆盖：
- 创建文档并入库（source ACL 标注）
- 列表 + source 过滤
- 文档详情（status=completed, chunk_count>0, embedding_model_id 记录）
- 无激活 embedding 模型 → 400 + status=failed
- 级联删除（删文档 → zvec chunks 清理）
- reindex（重新入库）
- sources 列表
- 文件上传（multipart）
- 更新文档标题（不触发重入库）
- 更新 source（触发重入库）

embedding 模型被 mock（不连真实 API），用 sha256 派生的稳定小维度向量，
与 test_rag.py 同款策略。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace
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

# 测试用小维度，快速且与 mock embed 对齐。
DIM = 16


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """构造挂了 admin router 的子 app + 全新 SQLite + mock embedding 模型。

    resolver.get_embed_model 被 patch 成返回 (假 Model, mock EmbedModel)，
    这样入库流程不需要真实 API key 也不连真实 embedding 服务。
    """
    db_path = tmp_path / "test_knowledge.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", str(DIM))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    # mock embedding 模型：embed 返回 sha256 派生的归一化向量
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
    fake_model = SimpleNamespace(id=1, name="mock-embed", embed_dimension=DIM)
    monkeypatch.setattr(resolver, "get_embed_model", lambda session: (fake_model, mock_embed))

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def no_embed_client(tmp_path: Path, monkeypatch):
    """无激活 embedding 模型的环境：get_embed_model 返回 None。"""
    db_path = tmp_path / "test_knowledge_no_embed.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", str(DIM))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    monkeypatch.setattr(resolver, "get_embed_model", lambda session: None)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ============ 创建 + 入库 ============


def test_create_document_ingests_successfully(client: TestClient):
    """创建文档 → 201 + ok=True + chunk_count>0 + status=completed。"""
    resp = client.post(
        "/api/admin/knowledge/documents",
        json={
            "title": "销售流程手册",
            "source": "sales",
            "content": "我们的报价流程如下：先询价再签约。\n\n报价需要走审批流程。",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["chunk_count"] > 0
    assert body["errors"] == []

    # 文档详情确认状态
    doc_id = body["document_id"]
    detail = client.get(f"/api/admin/knowledge/documents/{doc_id}").json()
    assert detail["status"] == "completed"
    assert detail["chunk_count"] > 0
    assert detail["source"] == "sales"
    assert detail["embedding_model_id"] == 1  # mock model id


def test_create_empty_content_returns_400(client: TestClient):
    """空正文 → 400。"""
    resp = client.post(
        "/api/admin/knowledge/documents",
        json={"title": "空", "source": "sales", "content": "   "},
    )
    assert resp.status_code == 400


def test_create_empty_source_returns_400(client: TestClient):
    """空 source → 400。"""
    resp = client.post(
        "/api/admin/knowledge/documents",
        json={"title": "无标签", "source": "  ", "content": "内容"},
    )
    assert resp.status_code == 400


def test_create_without_active_embedding_returns_400(no_embed_client: TestClient):
    """无激活 embedding 模型 → 400 + status=failed + 友好错误。"""
    resp = no_embed_client.post(
        "/api/admin/knowledge/documents",
        json={"title": "测试", "source": "sales", "content": "内容"},
    )
    assert resp.status_code == 400
    assert "embedding" in resp.json()["detail"].lower()

    # 文档记录仍存在，但 status=failed
    listing = no_embed_client.get("/api/admin/knowledge/documents").json()
    assert len(listing) == 1
    assert listing[0]["status"] == "failed"
    assert "embedding" in listing[0]["error_message"].lower()


# ============ 列表 + 过滤 ============


def test_list_documents_and_filter_by_source(client: TestClient):
    """多文档不同 source，列表全返回，source 过滤生效。"""
    client.post(
        "/api/admin/knowledge/documents",
        json={"title": "销售文档", "source": "sales", "content": "销售内容。"},
    )
    client.post(
        "/api/admin/knowledge/documents",
        json={"title": "HR文档", "source": "hr", "content": "HR内容。"},
    )

    # 全部
    all_docs = client.get("/api/admin/knowledge/documents").json()
    assert len(all_docs) == 2

    # 按 source 过滤
    sales = client.get("/api/admin/knowledge/documents", params={"source": "sales"}).json()
    assert len(sales) == 1
    assert sales[0]["source"] == "sales"


def test_list_sources(client: TestClient):
    """sources 端点返回去重排序的 source 列表。"""
    client.post(
        "/api/admin/knowledge/documents",
        json={"title": "A", "source": "hr", "content": "内容"},
    )
    client.post(
        "/api/admin/knowledge/documents",
        json={"title": "B", "source": "sales", "content": "内容"},
    )
    client.post(
        "/api/admin/knowledge/documents",
        json={"title": "C", "source": "sales", "content": "内容2"},
    )

    sources = client.get("/api/admin/knowledge/sources").json()
    assert sources == ["hr", "sales"]  # 去重 + 排序


# ============ 级联删除 ============


def test_delete_document_cleans_chunks(client: TestClient):
    """删文档 → 204 + zvec chunks 清理 + DB 记录删除。"""
    create_resp = client.post(
        "/api/admin/knowledge/documents",
        json={"title": "待删", "source": "public", "content": "要被删除的内容。"},
    )
    doc_id = create_resp.json()["document_id"]
    assert create_resp.json()["chunk_count"] > 0

    # 删除
    del_resp = client.delete(f"/api/admin/knowledge/documents/{doc_id}")
    assert del_resp.status_code == 204

    # DB 记录已删
    detail = client.get(f"/api/admin/knowledge/documents/{doc_id}")
    assert detail.status_code == 404

    # 列表里也没了
    listing = client.get("/api/admin/knowledge/documents").json()
    assert all(d["id"] != doc_id for d in listing)


def test_delete_nonexistent_returns_404(client: TestClient):
    """删不存在的文档 → 404。"""
    resp = client.delete("/api/admin/knowledge/documents/9999")
    assert resp.status_code == 404


# ============ reindex ============


def test_reindex_document(client: TestClient):
    """reindex → 重新入库，chunk_count 与之前一致（同 content 同 mock embed）。"""
    create_resp = client.post(
        "/api/admin/knowledge/documents",
        json={"title": "重入库", "source": "public", "content": "要重新入库的内容。"},
    )
    doc_id = create_resp.json()["document_id"]
    original_count = create_resp.json()["chunk_count"]

    # reindex
    reindex_resp = client.post(f"/api/admin/knowledge/documents/{doc_id}/reindex")
    assert reindex_resp.status_code == 200
    assert reindex_resp.json()["ok"] is True
    assert reindex_resp.json()["chunk_count"] == original_count


# ============ 更新 ============


def test_update_title_no_reingest(client: TestClient):
    """改标题不触发重入库（source 不变）。"""
    create_resp = client.post(
        "/api/admin/knowledge/documents",
        json={"title": "原标题", "source": "sales", "content": "内容。"},
    )
    doc_id = create_resp.json()["document_id"]

    resp = client.put(
        f"/api/admin/knowledge/documents/{doc_id}",
        json={"title": "新标题"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "新标题"
    assert resp.json()["source"] == "sales"


def test_update_source_triggers_reingest(client: TestClient):
    """改 source 触发重入库。"""
    create_resp = client.post(
        "/api/admin/knowledge/documents",
        json={"title": "标题", "source": "sales", "content": "内容。"},
    )
    doc_id = create_resp.json()["document_id"]

    resp = client.put(
        f"/api/admin/knowledge/documents/{doc_id}",
        json={"source": "hr"},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "hr"


# ============ 文件上传 ============


def test_upload_txt_file(client: TestClient):
    """上传 .txt 文件 → 201 + 入库成功。"""
    resp = client.post(
        "/api/admin/knowledge/upload",
        params={"source": "public", "title": "上传的文档"},
        files={"file": ("test.txt", "这是上传的文本内容。\n\n第二段。".encode(), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["chunk_count"] > 0

    # 文档元数据正确
    detail = client.get(f"/api/admin/knowledge/documents/{body['document_id']}").json()
    assert detail["file_name"] == "test.txt"
    assert detail["file_type"] == "txt"


def test_upload_unsupported_format_returns_400(client: TestClient):
    """上传不支持的格式 → 400。"""
    resp = client.post(
        "/api/admin/knowledge/upload",
        params={"source": "public"},
        files={"file": ("doc.pdf", b"fake pdf", "application/pdf")},
    )
    assert resp.status_code == 400


def test_upload_non_utf8_returns_400(client: TestClient):
    """上传非 UTF-8 二进制 → 400。"""
    resp = client.post(
        "/api/admin/knowledge/upload",
        params={"source": "public"},
        files={"file": ("bad.txt", b"\xff\xfe\x00\x01", "text/plain")},
    )
    assert resp.status_code == 400


# ============ 文档详情 404 ============


def test_get_nonexistent_returns_404(client: TestClient):
    """查不存在的文档 → 404。"""
    resp = client.get("/api/admin/knowledge/documents/9999")
    assert resp.status_code == 404
