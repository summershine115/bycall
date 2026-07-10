"""Model 实体管理 Admin REST API 单元测试。

覆盖：
- provider 注册表端点（含 model_type 过滤）
- 创建 / 列表 / 详情（api_key 脱敏，不回显明文）
- 同类型下重名 → 409
- 激活的同类型原子切换
- 删除 is_active 模型 → 400
- embedding 激活的维度检测（need_rebuild + rebuild=true 放行）
- 更新时 api_key 脱敏占位符跳过
- 连通性测试假地址 → ok=False（不抛 500）

fixture 复用 test_admin_api.py 的临时 SQLite + init_db 模式。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, set_engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    """构造挂了 admin router 的子 app + 全新 SQLite。"""
    db_path = tmp_path / "test_models.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _create_chat(client: TestClient, name: str = "glm-4-flash", **overrides) -> dict:
    """便利：建一个 chat 模型，返回响应体。"""
    payload = {
        "name": name,
        "model_type": "chat",
        "provider": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "sk-test-secret-key-1234567890",
        "is_active": False,
    }
    payload.update(overrides)
    resp = client.post("/api/admin/models", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ============ provider 注册表 ============


def test_providers_endpoint_returns_list_and_filters_by_type(client: TestClient):
    """GET /models/providers 返回非空列表；model_type 过滤生效。"""
    resp = client.get("/api/admin/models/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) > 0  # 注册表非空
    assert all("value" in p and "label" in p for p in body)

    # 过滤 chat：至少含 openai/zhipu 等支持 chat 的
    chat = client.get("/api/admin/models/providers", params={"model_type": "chat"}).json()
    assert any(p["value"] == "openai" for p in chat)
    # deepseek 只支持 chat，不该出现在 embedding 过滤里
    embed = client.get("/api/admin/models/providers", params={"model_type": "embedding"}).json()
    assert all("embedding" in p["model_types"] for p in embed)
    assert all(p["value"] != "deepseek" for p in embed)


# ============ 创建 / 列表 / 脱敏 ============


def test_create_model_then_list_api_key_masked(client: TestClient):
    """创建 chat 模型 → 201；列表能看到；响应只给 masked + has_api_key，无明文。"""
    body = _create_chat(client, api_key="sk-super-secret-key-abcdef")
    assert body["name"] == "glm-4-flash"
    assert body["has_api_key"] is True
    assert body["api_key_masked"]  # 非空脱敏串
    # 绝不回显明文
    assert "sk-super-secret-key-abcdef" not in body["api_key_masked"]
    assert "api_key" not in body  # 字段名都不该出现明文 key

    listing = client.get("/api/admin/models", params={"model_type": "chat"}).json()
    assert any(m["name"] == "glm-4-flash" for m in listing)
    # 全部条目都不含明文密钥
    assert all("sk-super-secret-key-abcdef" not in str(m) for m in listing)


def test_create_duplicate_name_within_same_type_returns_409(client: TestClient):
    """同类型下重名 → 409；不同类型同名应允许（不在本断言范围）。"""
    _create_chat(client, name="dup-model")
    resp = client.post(
        "/api/admin/models",
        json={"name": "dup-model", "model_type": "chat"},
    )
    assert resp.status_code == 409


# ============ 激活：同类型原子切换 ============


def test_activate_chat_swaps_active_within_type(client: TestClient):
    """激活 chat A → is_active=True；再激活 B → A 变 False，B 为 True。"""
    a = _create_chat(client, name="chat-a")
    aid = a["id"]

    # 激活 A
    act_a = client.post(f"/api/admin/models/{aid}/activate")
    assert act_a.status_code == 200, act_a.text
    assert act_a.json()["ok"] is True
    assert act_a.json()["model"]["is_active"] is True

    # 建 B 并激活
    b = _create_chat(client, name="chat-b")
    bid = b["id"]
    act_b = client.post(f"/api/admin/models/{bid}/activate").json()
    assert act_b["model"]["is_active"] is True

    # 列表里 A 应已变 False
    listing = {
        m["id"]: m for m in client.get("/api/admin/models", params={"model_type": "chat"}).json()
    }
    assert listing[aid]["is_active"] is False
    assert listing[bid]["is_active"] is True


# ============ 删除激活模型 → 400 ============


def test_delete_active_model_returns_400(client: TestClient):
    """删 is_active=True 的模型 → 400（需先激活其他再删）。"""
    m = _create_chat(client, name="active-one", is_active=True)
    resp = client.delete(f"/api/admin/models/{m['id']}")
    assert resp.status_code == 400
    # 未激活的可以删
    other = _create_chat(client, name="inactive-one", is_active=False)
    assert client.delete(f"/api/admin/models/{other['id']}").status_code == 204


# ============ embedding 激活维度检测 ============


def test_embedding_activate_dimension_check(client: TestClient):
    """已有 dim=1536 active，激活 dim=1024 的：先 need_rebuild=True，带 rebuild=true 放行。"""
    # 建 dim=1536 的 embedding 并直接激活
    e1 = client.post(
        "/api/admin/models",
        json={
            "name": "embed-1536",
            "model_type": "embedding",
            "embed_dimension": 1536,
            "is_active": True,
        },
    )
    assert e1.status_code == 201, e1.text
    e1_id = e1.json()["id"]

    # 建 dim=1024 的 embedding（未激活）
    e2 = client.post(
        "/api/admin/models",
        json={
            "name": "embed-1024",
            "model_type": "embedding",
            "embed_dimension": 1024,
            "is_active": False,
        },
    )
    assert e2.status_code == 201, e2.text
    e2_id = e2.json()["id"]

    # 激活 e2：维度变化 → need_rebuild=True，不真正切换
    first = client.post(f"/api/admin/models/{e2_id}/activate")
    assert first.status_code == 200
    fb = first.json()
    assert fb["need_rebuild"] is True
    assert fb["old_dim"] == 1536
    assert fb["new_dim"] == 1024

    # 此时 e1 仍 active（未真正切换）
    listing = {
        m["id"]: m
        for m in client.get("/api/admin/models", params={"model_type": "embedding"}).json()
    }
    assert listing[e1_id]["is_active"] is True
    assert listing[e2_id]["is_active"] is False

    # 带 rebuild=true → 真正激活
    second = client.post(f"/api/admin/models/{e2_id}/activate", params={"rebuild": "true"})
    assert second.status_code == 200
    sb = second.json()
    assert sb["ok"] is True
    assert sb["model"]["is_active"] is True

    # e1 应变 False
    listing = {
        m["id"]: m
        for m in client.get("/api/admin/models", params={"model_type": "embedding"}).json()
    }
    assert listing[e1_id]["is_active"] is False
    assert listing[e2_id]["is_active"] is True


# ============ 更新：脱敏占位符跳过 ============


def test_update_api_key_with_masked_placeholder_is_skipped(client: TestClient):
    """PUT api_key 传含 *** 的脱敏占位符 → 跳过（原 key 不变）。"""
    m = _create_chat(client, name="key-test", api_key="sk-original-real-key-xxx")
    mid = m["id"]
    original_masked = m["api_key_masked"]

    # 传脱敏占位符（前端原样回填的展示值）
    resp = client.put(
        f"/api/admin/models/{mid}",
        json={"api_key": original_masked, "display_name": "新名"},
    )
    assert resp.status_code == 200, resp.text
    # 其他字段照常更新
    assert resp.json()["display_name"] == "新名"

    # 再 PUT 一个真实新 key → 应该改
    resp2 = client.put(
        f"/api/admin/models/{mid}",
        json={"api_key": "sk-brand-new-real-key-yyy"},
    )
    assert resp2.status_code == 200
    # 脱敏值应该变化（说明 key 被真改了）
    assert resp2.json()["api_key_masked"] != original_masked


# ============ 连通性测试：假地址 → ok=False ============


def test_test_endpoint_with_fake_url_returns_ok_false(client: TestClient):
    """test 端点打到假 base_url → ok=False + error，不抛 500。"""
    m = _create_chat(client, name="fake-model", base_url="http://127.0.0.1:1/v1")
    resp = client.post(f"/api/admin/models/{m['id']}/test")
    assert resp.status_code == 200  # 不抛 500
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]  # 有错误信息
