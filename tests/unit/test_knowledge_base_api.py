"""知识库（KnowledgeBase）CRUD Admin REST API 单元测试。

覆盖：
- 创建 KB（读 embedding 模型维度 → 建 zvec collection → collection_name=kb_{id}）
- name 唯一冲突 409
- embedding 模型不存在 → 400
- embedding 模型类型不对（chat 而非 embedding）→ 400
- embedding 模型 embed_dimension=0 → 400
- 列表
- 详情（200 / 404）
- 更新名称/描述
- 删除级联（collection destroy + 文档删除）

KB 创建不调用 embedding API（只在文档入库时才用），所以这里不需要 mock embedding。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, set_engine

DIM = 16


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """挂载 admin router 的子 app + 全新 SQLite。"""
    db_path = tmp_path / "test_kb.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", str(DIM))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _create_embedding_model(client: TestClient, dim: int = DIM, name: str = "mock-embed") -> int:
    """通过 API 创建一个 embedding 类型 Model 记录，返回 id。"""
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


def _create_kb(client: TestClient, embed_model_id: int, name: str = "测试知识库"):
    resp = client.post(
        "/api/admin/knowledge-bases",
        json={
            "name": name,
            "description": "测试描述",
            "embedding_model_id": embed_model_id,
        },
    )
    return resp


# ============ 创建 ============


def test_create_kb_success(client: TestClient):
    """创建 KB → 201 + collection_name=kb_{id} + dimension 来自 embedding 模型。"""
    mid = _create_embedding_model(client, dim=256, name="embed-256")
    resp = _create_kb(client, mid, name="产品知识库")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "产品知识库"
    assert body["description"] == "测试描述"
    assert body["embedding_model_id"] == mid
    assert body["dimension"] == 256
    assert body["collection_name"] == f"kb_{body['id']}"
    assert body["document_count"] == 0
    assert body["chunk_count"] == 0


def test_create_kb_duplicate_name_409(client: TestClient):
    """重名 → 409。"""
    mid = _create_embedding_model(client)
    _create_kb(client, mid, name="唯一库")
    resp = _create_kb(client, mid, name="唯一库")
    assert resp.status_code == 409


def test_create_kb_bad_embedding_model_400(client: TestClient):
    """embedding_model_id 不存在 → 400。"""
    resp = _create_kb(client, 9999, name="空库")
    assert resp.status_code == 400


def test_create_kb_wrong_model_type_400(client: TestClient):
    """embedding_model_id 指向的是 chat 模型 → 400。"""
    # 建一个 chat 模型
    chat_resp = client.post(
        "/api/admin/models",
        json={"name": "chat-1", "model_type": "chat", "is_active": True},
    )
    assert chat_resp.status_code == 201
    chat_id = chat_resp.json()["id"]

    resp = _create_kb(client, chat_id, name="错类型")
    assert resp.status_code == 400


def test_create_kb_zero_dimension_400(client: TestClient):
    """embedding 模型 embed_dimension=0 → 400。"""
    mid = _create_embedding_model(client, dim=0, name="embed-zero")
    resp = _create_kb(client, mid, name="零维库")
    assert resp.status_code == 400


# ============ 列表 + 详情 ============


def test_list_kbs(client: TestClient):
    """列出全部 KB。"""
    mid = _create_embedding_model(client)
    _create_kb(client, mid, name="库A")
    _create_kb(client, mid, name="库B")

    listing = client.get("/api/admin/knowledge-bases").json()
    names = sorted(kb["name"] for kb in listing)
    assert names == ["库A", "库B"]


def test_get_kb_success(client: TestClient):
    """按 id 取 KB 详情。"""
    mid = _create_embedding_model(client)
    created = _create_kb(client, mid, name="详情库").json()
    resp = client.get(f"/api/admin/knowledge-bases/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "详情库"


def test_get_kb_not_found_404(client: TestClient):
    """不存在 → 404。"""
    assert client.get("/api/admin/knowledge-bases/9999").status_code == 404


# ============ 更新 ============


def test_update_kb_name(client: TestClient):
    """更新名称 → 200。"""
    mid = _create_embedding_model(client)
    kb = _create_kb(client, mid, name="旧名").json()
    resp = client.put(f"/api/admin/knowledge-bases/{kb['id']}", json={"name": "新名"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名"


def test_update_kb_name_conflict_409(client: TestClient):
    """更新名称到已有名 → 409。"""
    mid = _create_embedding_model(client)
    _create_kb(client, mid, name="占用名")
    kb = _create_kb(client, mid, name="原名").json()
    resp = client.put(f"/api/admin/knowledge-bases/{kb['id']}", json={"name": "占用名"})
    assert resp.status_code == 409


def test_update_kb_description(client: TestClient):
    """更新描述 → 200。"""
    mid = _create_embedding_model(client)
    kb = _create_kb(client, mid, name="描述库").json()
    resp = client.put(f"/api/admin/knowledge-bases/{kb['id']}", json={"description": "新描述"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "新描述"


# ============ 删除 ============


def test_delete_kb_success(client: TestClient):
    """删 KB → 204 + 列表里消失。"""
    mid = _create_embedding_model(client)
    kb = _create_kb(client, mid, name="待删库").json()

    resp = client.delete(f"/api/admin/knowledge-bases/{kb['id']}")
    assert resp.status_code == 204

    # 详情 404
    assert client.get(f"/api/admin/knowledge-bases/{kb['id']}").status_code == 404
    # 列表里没了
    listing = client.get("/api/admin/knowledge-bases").json()
    assert all(k["id"] != kb["id"] for k in listing)


def test_delete_kb_not_found_404(client: TestClient):
    """删不存在的 KB → 404。"""
    assert client.delete("/api/admin/knowledge-bases/9999").status_code == 404
