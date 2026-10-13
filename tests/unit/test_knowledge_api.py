"""知识库文档管理 Admin REST API 单元测试（per-KB 版，异步入库 + 实时进度）。

覆盖：
- 创建文档并异步入库（source ACL 标注，绑定到 KB）
- 列表 + source 过滤
- 文档详情（status=completed, chunk_count>0, embedding_model_id 记录）
- 无绑定 embedding 模型 → 入库失败 status=failed
- 级联删除（删文档 → zvec chunks 清理）
- reindex（重新入库）
- sources 列表
- 文件上传（multipart，同步解析 + 异步入库）
- 更新文档标题（不触发重入库）
- 更新 source（触发同步重入库，含 ACL 回滚）
- 入库实时进度 /progress（阶段切换、终态 percent=100）
- /ingest/active 列出在途入库
- 失败终态：status=failed + error_message，绝不卡在 processing

入库已异步化：create/upload/reindex 立即返回 IngestAccepted，真正入库在后台线程跑。
``_wait_for_ingest`` 轮询 /progress 直到终态后再断言 chunk_count/status。
``resolve_embed_model`` 被 mock（返回 sha256 派生的稳定小维度向量），无需真实 embedding 服务。
"""

from __future__ import annotations

import hashlib
import math
import time
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
    SQLite 加 busy timeout，兼容后台入库线程的并发写。
    """
    db_path = tmp_path / "test_knowledge.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", str(DIM))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    get_settings.cache_clear()

    set_engine(
        create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    )
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
    test_client = TestClient(app)
    yield test_client
    # 用例结束前排空本用例提交的后台入库任务，避免残留线程跨用例写旧 engine/污染 tracker。
    from bycall.admin.api import _drain_ingest

    _drain_ingest(timeout=30)


@pytest.fixture(autouse=True)
def _clear_ingest_tracker():
    """每个用例前后清空内存进度表（模块级单例，需跨用例隔离）。"""
    from bycall.rag.ingest_tracker import ingest_tracker

    with ingest_tracker._lock:
        ingest_tracker._items.clear()
    yield
    with ingest_tracker._lock:
        ingest_tracker._items.clear()


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
    """在指定 KB 下建文档并提交异步入库（返回 IngestAccepted）。"""
    return client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents",
        json={
            "knowledge_base_id": kb_id,
            "title": title,
            "source": source,
            "content": content,
        },
    )


def _wait_for_ingest(client: TestClient, kb_id: int, doc_id: int, timeout: float = 10.0) -> dict:
    """轮询 /progress 直到 status 终态（completed/failed），返回最终进度。"""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/progress")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"入库未在 {timeout}s 内到达终态: {last}")


# ============ 创建 + 入库 ============


def test_create_document_ingests_successfully(client: TestClient):
    """创建文档 → 201 + status=processing（受理），轮询后 completed + chunk_count>0。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = _create_doc(client, kb_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "processing"
    doc_id = body["document_id"]

    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "completed"
    assert prog["chunk_count"] > 0
    assert prog["percent"] == 100

    # 文档详情确认状态
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["status"] == "completed"
    assert detail["chunk_count"] > 0
    assert detail["source"] == "sales"
    assert detail["embedding_model_id"] == mid


def test_create_empty_content_returns_400(client: TestClient):
    """空正文 → 400（同步校验，不创建文档、不提交后台任务）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = _create_doc(client, kb_id, content="   ")
    assert resp.status_code == 400
    # 同步失败：没有后台入库任务在途
    assert client.get("/api/admin/ingest/active").json() == []


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

    # 全部（文档建好即返回，不依赖入库完成）
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
    # 先等入库完成拿到 chunk_count（异步入库，不能从受理响应直接读）
    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "completed"
    chunk_count = prog["chunk_count"]
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
    original = _wait_for_ingest(client, kb_id, doc_id)
    original_count = original["chunk_count"]

    reindex_resp = client.post(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reindex")
    assert reindex_resp.status_code == 200
    assert reindex_resp.json()["status"] == "processing"

    reindexed = _wait_for_ingest(client, kb_id, doc_id)
    assert reindexed["status"] == "completed"
    assert reindexed["chunk_count"] == original_count


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
    original = _wait_for_ingest(client, kb_id, doc_id)
    original_count = original["chunk_count"]
    before = _get_zvec_collection_doc_count(kb_id)
    assert before == original_count, f"初始文档数应为 {original_count}，实际 {before}"

    # reindex
    reindex_resp = client.post(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reindex")
    assert reindex_resp.status_code == 200
    reindexed = _wait_for_ingest(client, kb_id, doc_id)
    assert reindexed["status"] == "completed"

    # 关键：reindex 后 chunk 数应等于 original_count（upsert 覆盖，不是 insert 追加）
    assert reindexed["chunk_count"] == original_count
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
    _wait_for_ingest(client, kb_id, doc_id)

    resp = client.put(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}",
        json={"title": "新标题"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "新标题"
    assert resp.json()["source"] == "sales"


def test_update_source_triggers_reingest(client: TestClient):
    """改 source 触发同步重入库（保持即时回滚语义）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    create_resp = _create_doc(client, kb_id, title="标题", source="sales", content="内容。")
    doc_id = create_resp.json()["document_id"]
    _wait_for_ingest(client, kb_id, doc_id)

    resp = client.put(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}",
        json={"source": "hr"},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "hr"


# ============ 文件上传 ============


def test_upload_txt_file(client: TestClient):
    """上传 .txt 文件 → 201 + 受理，轮询后入库成功 + 元数据正确。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public", "title": "上传的文档"},
        files={"file": ("test.txt", "这是上传的文本内容。\n\n第二段。".encode(), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "processing"
    doc_id = body["document_id"]

    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "completed"
    assert prog["chunk_count"] > 0

    # 文档元数据正确
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["file_name"] == "test.txt"
    assert detail["file_type"] == "txt"


def test_upload_unsupported_format_returns_400(client: TestClient):
    """上传不支持的扩展名 → 400（扩展名同步校验，不进后台）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public"},
        files={"file": ("doc.exe", b"binary", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_non_utf8_fails_in_background(client: TestClient):
    """上传非 UTF-8 文本 → 受理 201，解析在后台失败 → status=failed（异步解析契约）。

    解析已异步化：内容级错误（编码/格式）不再立即 4xx，而是后台落 failed 终态，
    经 /progress 暴露错误信息（参考 WeKnora）。
    """
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public"},
        files={"file": ("bad.txt", b"\xff\xfe\x00\x01", "text/plain")},
    )
    assert resp.status_code == 201
    doc_id = resp.json()["document_id"]
    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "failed"
    assert "UTF-8" in prog["error_message"]


def test_upload_invalid_pdf_fails_in_background(client: TestClient):
    """上传无效 PDF 字节 → 受理 201，后台解析失败 → failed（原同步 4xx 现为异步终态）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public"},
        files={"file": ("bad.pdf", b"not a real pdf", "application/pdf")},
    )
    assert resp.status_code == 201
    doc_id = resp.json()["document_id"]
    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "failed"


def test_upload_unavailable_engine_returns_400(client: TestClient, monkeypatch):
    """显式选不可用引擎（markitdown 未装）→ 400 配置错误（廉价预检，不进后台）。"""
    from bycall.rag.parsers.engines import MarkItDownEngine

    monkeypatch.setattr(MarkItDownEngine, "is_available", lambda self: False)
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public", "engine": "markitdown"},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    assert "markitdown" in resp.json()["detail"]


def test_concurrent_uploads_parse_in_parallel(client: TestClient, monkeypatch):
    """多文件并发上传：解析在后台线程池并行，互不阻塞（核心异步契约，参考 WeKnora）。

    断言两件事：(1) 上传提交几乎立即返回（不等慢解析）；(2) 多个解析确有重叠（并行）。
    """
    import threading
    import time as _time

    import bycall.rag.parsers as parsers
    from bycall.rag.parsers.base import ParseResult

    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def slow_parse(content, file_name="", *, engine=None, settings=None):
        """模拟慢解析（如 mineru），记录并发重叠数。"""
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        _time.sleep(0.3)
        with lock:
            active["n"] -= 1
        return ParseResult(text=f"{file_name} 解析正文", title=file_name)

    monkeypatch.setattr(parsers, "parse_document", slow_parse)

    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)

    t0 = _time.monotonic()
    doc_ids = []
    for i in range(3):
        resp = client.post(
            f"/api/admin/knowledge-bases/{kb_id}/upload",
            params={"source": "public"},
            files={"file": (f"f{i}.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 201, resp.text
        doc_ids.append(resp.json()["document_id"])
    # 提交立即返回：3 个慢解析若串行需 ~0.9s，异步提交应远小于此。
    assert _time.monotonic() - t0 < 0.6

    for doc_id in doc_ids:
        prog = _wait_for_ingest(client, kb_id, doc_id)
        assert prog["status"] == "completed"

    # 关键：解析确实并行（最大并发 ≥ 2），而非排队串行。
    assert active["max"] >= 2, f"解析未并行执行，最大并发={active['max']}"


def test_kb_parse_engine_map_persists(client: TestClient):
    """KB 设置保存 parse_engine_map → 回读一致；空 KB 默认 None。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)

    # 初始无映射
    kb = client.get(f"/api/admin/knowledge-bases/{kb_id}").json()
    assert kb["parse_engine_map"] in (None, {})

    # 保存 per-type 映射
    engine_map = {"default": "builtin", "pdf": "mineru", "docx": "markitdown"}
    resp = client.put(
        f"/api/admin/knowledge-bases/{kb_id}",
        json={"parse_engine_map": engine_map},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parse_engine_map"] == engine_map

    # 回读一致
    kb = client.get(f"/api/admin/knowledge-bases/{kb_id}").json()
    assert kb["parse_engine_map"] == engine_map


def test_delete_document_keeps_metadata_when_vector_delete_fails(client: TestClient, monkeypatch):
    """向量删除失败必须 fail-closed，不能制造仍可检索的幽灵 chunk。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    created = _create_doc(client, kb_id, title="保留元数据", source="public", content="内容")
    doc_id = created.json()["document_id"]
    _wait_for_ingest(client, kb_id, doc_id)

    import bycall.admin.api as admin_api

    monkeypatch.setattr(
        admin_api,
        "_delete_chunks_for_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("zvec unavailable")),
    )
    response = client.delete(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}")
    assert response.status_code == 503
    assert client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").status_code == 200


def test_source_change_rolls_back_when_embedding_fails(client: TestClient, monkeypatch):
    """ACL 更新失败时 DB source 必须回滚到旧授权状态（同步路径保持即时回滚）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    created = _create_doc(client, kb_id, title="ACL 文档", source="public", content="内容")
    doc_id = created.json()["document_id"]
    _wait_for_ingest(client, kb_id, doc_id)

    monkeypatch.setattr(
        resolver,
        "resolve_embed_model",
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
    _wait_for_ingest(client, kb_id, doc_id)
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
    _wait_for_ingest(client, kb_a, doc_id)
    resp = client.get(f"/api/admin/knowledge-bases/{kb_b}/documents/{doc_id}/content")
    assert resp.status_code == 404


# ============ 入库失败终态：status=failed + error_message（绝不卡在 processing）===========


def test_ingest_failure_records_error_message(client: TestClient, monkeypatch):
    """embedding 失败 → 文档 status=failed 且 error_message 非空，不卡在 processing。

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
    assert resp.status_code == 201  # 文档已创建并受理
    doc_id = resp.json()["document_id"]

    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "failed"
    assert prog["error_message"]

    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["status"] == "failed"
    assert "embedding" in detail["error_message"]


def test_failed_ingest_never_stuck_in_processing(client: TestClient, monkeypatch):
    """入库失败后 status 一定是 failed，绝不留在 processing（无僵尸文档）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)

    failing = MagicMock(spec=EmbedModel)
    failing.dimension = DIM
    failing.embed.side_effect = RuntimeError("boom")
    monkeypatch.setattr(resolver, "resolve_embed_model", lambda session, model: failing)

    doc_id = _create_doc(client, kb_id, content="会失败的正文。").json()["document_id"]
    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "failed"
    assert prog["status"] != "processing"
    # tracker 也应标记 failed（实时 stage）
    assert prog["stage"] == "failed"


def test_upload_failure_records_error_message(client: TestClient, monkeypatch):
    """文件上传入库失败 → status=failed + error_message（解析同步、入库异步失败也落终态）。"""
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
    doc_id = resp.json()["document_id"]
    prog = _wait_for_ingest(client, kb_id, doc_id)
    assert prog["status"] == "failed"
    detail = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_message"]


# ============ 实时进度 /progress + /ingest/active ============


def _slow_embed_factory(delay: float):
    """构造一个会延迟 delay 秒的 mock embedding，便于在途观测进度。"""
    slow = MagicMock(spec=EmbedModel)
    slow.dimension = DIM

    def embed(texts: list[str]) -> list[list[float]]:
        time.sleep(delay)
        return [[0.0] * DIM for _ in texts]

    slow.embed.side_effect = embed
    return slow


def test_progress_endpoint_reports_stages_and_final_percent(client: TestClient, monkeypatch):
    """/progress 在途阶段非空，完成后 percent=100、stage=done。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    monkeypatch.setattr(
        resolver, "resolve_embed_model", lambda session, model: _slow_embed_factory(0.3)
    )

    resp = _create_doc(client, kb_id, content="一段会被慢速向量化的较长正文。\n" * 5)
    assert resp.status_code == 201
    doc_id = resp.json()["document_id"]

    # 轮询抓取在途阶段（start() 后短暂为 ""，工作线程很快进入 chunking/embedding）。
    seen_stage = ""
    poll_deadline = time.monotonic() + 3.0
    while time.monotonic() < poll_deadline:
        prog = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/progress").json()
        if prog["stage"]:
            seen_stage = prog["stage"]
            break
        time.sleep(0.02)
    assert seen_stage in ("chunking", "embedding", "indexing"), seen_stage

    final = _wait_for_ingest(client, kb_id, doc_id)
    assert final["status"] == "completed"
    assert final["percent"] == 100
    assert final["stage"] == "done"


def test_ingest_active_lists_processing_doc(client: TestClient, monkeypatch):
    """/ingest/active 在入库在途时列出该文档。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    monkeypatch.setattr(
        resolver, "resolve_embed_model", lambda session, model: _slow_embed_factory(0.5)
    )

    resp = _create_doc(client, kb_id, content="一段会被慢速向量化的文本。")
    doc_id = resp.json()["document_id"]

    # 轮询直到 active 列出该文档（工作线程进入 chunking/embedding/indexing 才算 active）。
    matched = None
    poll_deadline = time.monotonic() + 3.0
    while time.monotonic() < poll_deadline:
        active = client.get("/api/admin/ingest/active").json()
        matched = next((a for a in active if a["document_id"] == doc_id), None)
        if matched is not None:
            break
        time.sleep(0.02)
    assert matched is not None, "入库在途期间未在 /ingest/active 中出现"
    assert matched["stage"] in ("chunking", "embedding", "indexing")
    assert matched["status"] == "processing"

    # 完成后 active 不再包含
    _wait_for_ingest(client, kb_id, doc_id)
    active_after = client.get("/api/admin/ingest/active").json()
    assert not any(a["document_id"] == doc_id for a in active_after)


def test_progress_processing_without_tracker_suggests_rebuild(client: TestClient):
    """DB 标 processing 但无 tracker 记录（服务重启中途）→ 提示重建，不假装有进度。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    # 直接构造一个卡在 processing 的文档（模拟服务重启中途丢失 tracker）
    from bycall.db import session_scope
    from bycall.governance.models import Document

    with session_scope() as s:
        s.add(
            Document(
                knowledge_base_id=kb_id,
                title="僵尸文档",
                source="public",
                content="x",
                status="processing",
            )
        )
    # 取出 doc_id
    listing = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents").json()
    doc_id = listing[0]["id"]

    prog = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/progress").json()
    assert prog["status"] == "processing"
    assert prog["stage"] == ""
    assert "重建" in prog["error_message"]


def test_batch_reindex_submits_async(client: TestClient):
    """批量重入库 → 返回受理列表，每个文档各自异步完成。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    d1 = _create_doc(client, kb_id, title="甲", source="public", content="文档一内容。").json()[
        "document_id"
    ]
    d2 = _create_doc(client, kb_id, title="乙", source="public", content="文档二内容。").json()[
        "document_id"
    ]
    _wait_for_ingest(client, kb_id, d1)
    _wait_for_ingest(client, kb_id, d2)

    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents/batch-reindex",
        json={"document_ids": [d1, d2]},
    )
    assert resp.status_code == 200, resp.text
    accepted = resp.json()
    assert len(accepted) == 2
    assert {a["document_id"] for a in accepted} == {d1, d2}
    assert all(a["status"] == "processing" for a in accepted)

    # 两个都应异步完成
    for did in (d1, d2):
        assert _wait_for_ingest(client, kb_id, did)["status"] == "completed"


def test_batch_reindex_unknown_doc_returns_404(client: TestClient):
    """批量重入库含不存在文档 → 404（fail-fast，不静默丢弃）。"""
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    d1 = _create_doc(client, kb_id, content="内容。").json()["document_id"]
    _wait_for_ingest(client, kb_id, d1)

    resp = client.post(
        f"/api/admin/knowledge-bases/{kb_id}/documents/batch-reindex",
        json={"document_ids": [d1, 9999]},
    )
    assert resp.status_code == 404


def _managed_hits(kb_id, tags, level=0):
    from bycall.db import session_scope
    from bycall.governance.models import KnowledgeBase
    from bycall.rag.retrieve import retrieve_multi_kb

    with session_scope() as session:
        kb = session.get(KnowledgeBase, kb_id)
        return retrieve_multi_kb(
            session,
            get_settings().vector_store,
            [(kb.id, kb.collection_name, kb.dimension, kb.embedding_model_id)],
            "内容",
            tags,
            level=level,
        )


def test_acl_failure_never_publishes_partial_permissive_vectors(client, monkeypatch):
    """降密失败即使写入了部分低密级向量，也只能检索旧代。"""
    from bycall.vector_store.models import InsertResult
    from bycall.vector_store.zvec_store import ZvecStore

    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    created = _create_doc(client, kb_id, source="public", content="机密内容。" * 250)
    doc_id = created.json()["document_id"]
    _wait_for_ingest(client, kb_id, doc_id)
    assert (
        client.put(
            f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}", json={"level": 3}
        ).status_code
        == 200
    )
    before = {r.id for r in _managed_hits(kb_id, ["public"], level=3)}
    assert before
    original = ZvecStore.upsert

    def partial_write(self, chunks):
        original(self, chunks[:1])
        return [InsertResult(id=c.id, ok=False, message="模拟后续写入失败") for c in chunks]

    monkeypatch.setattr(ZvecStore, "upsert", partial_write)
    response = client.put(
        f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}", json={"level": 0}
    )
    assert response.status_code == 503
    doc = client.get(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}").json()
    assert doc["level"] == 3 and doc["status"] == "completed"
    assert not _managed_hits(kb_id, ["public"], level=0)
    assert {r.id for r in _managed_hits(kb_id, ["public"], level=3)} == before


def test_unpublished_generation_survives_cleanup_failure_without_visibility(client, monkeypatch):
    """模拟中断后的未发布向量；清理故障不影响新代 ACL 与可见性。"""
    from bycall.db import session_scope
    from bycall.governance.models import Document, KnowledgeBase
    from bycall.rag.ingest import IngestPipeline
    from bycall.rag.publication import stage_generation
    from bycall.vector_store.factory import create_vector_store
    from bycall.vector_store.zvec_store import ZvecStore

    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    doc_id = _create_doc(client, kb_id, source="public").json()["document_id"]
    _wait_for_ingest(client, kb_id, doc_id)
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        kb = session.get(KnowledgeBase, kb_id)
        abandoned = stage_generation(session, doc)
        store = create_vector_store(get_settings().vector_store, dimension=DIM)
        try:
            pipeline = IngestPipeline(
                store,
                resolver.resolve_embed_model(session, None),
                collection_name=kb.collection_name,
            )
            assert pipeline.ingest_text("中断留下的内容", source="public", doc_id=abandoned).success
        finally:
            store.close()
    assert all(not r.id.startswith(abandoned) for r in _managed_hits(kb_id, ["public"]))
    monkeypatch.setattr(
        ZvecStore, "delete_by_filter", MagicMock(side_effect=RuntimeError("清理故障"))
    )
    assert (
        client.put(
            f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}", json={"source": "hr"}
        ).status_code
        == 200
    )
    assert not _managed_hits(kb_id, ["public"])
    assert _managed_hits(kb_id, ["hr"])


def test_reindex_statistics_do_not_accumulate_characters(client):
    mid = _create_embedding_model(client)
    kb_id = _create_kb(client, mid)
    doc_id = _create_doc(client, kb_id).json()["document_id"]
    _wait_for_ingest(client, kb_id, doc_id)
    before = client.get(f"/api/admin/knowledge-bases/{kb_id}").json()
    client.post(f"/api/admin/knowledge-bases/{kb_id}/documents/{doc_id}/reindex")
    _wait_for_ingest(client, kb_id, doc_id)
    after = client.get(f"/api/admin/knowledge-bases/{kb_id}").json()
    assert after["total_chars"] == before["total_chars"]
    assert after["chunk_count"] == before["chunk_count"]
