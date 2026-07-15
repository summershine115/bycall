"""Admin REST API 单元测试。

用 FastAPI TestClient + 临时 SQLite，覆盖用户/角色/提示词/审计的核心路径：
- 正常 CRUD
- 软删除 vs 物理
- 唯一约束 → 409
- 保留字 / 禁删 admin → 400
- 被引用角色删除 → 409
- 提示词激活的原子切换

每个用例独立一份 DB（tmp_path 隔离），seed 后默认含一个 admin 账号 + member 角色 + v1 提示词。
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
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()

    # 注入隔离引擎并建表 + 灌默认数据（admin/member/v1 提示词）。
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed_admin_id(client: TestClient) -> int:
    """从列表里拿到 seed 的 admin 用户 id（删 admin 用例需要）。"""
    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    for u in resp.json():
        if u["name"] == "admin":
            return u["id"]
    raise AssertionError("seed 未生成 admin 账号")


# ============ 用户 ============


def test_create_user_then_list_no_password_hash(client: TestClient):
    """创建用户 → 201，列表能看到，响应不含 password_hash。"""
    resp = client.post(
        "/api/admin/users",
        json={
            "name": "alice",
            "display_name": "爱丽丝",
            "role": "member",
            "password": "secret123",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "alice"
    assert body["role"] == "member"
    assert body["is_active"] is True
    assert "password_hash" not in body  # 绝不泄漏
    assert "password" not in body

    # 列表里能拿到
    listing = client.get("/api/admin/users").json()
    names = [u["name"] for u in listing]
    assert "alice" in names
    # 全部条目都不含敏感字段
    assert all("password_hash" not in u for u in listing)


def test_create_duplicate_user_returns_409(client: TestClient):
    """重复 name → 409。"""
    payload = {"name": "bob", "role": "member"}
    assert client.post("/api/admin/users", json=payload).status_code == 201
    resp = client.post("/api/admin/users", json=payload)
    assert resp.status_code == 409


def test_patch_user_password_and_fields(client: TestClient):
    """PATCH 改密码 + display_name，再 GET 确认更新（且仍不泄漏 hash）。"""
    create = client.post(
        "/api/admin/users",
        json={"name": "carol", "display_name": "旧名", "password": "password1"},
    ).json()
    uid = create["id"]

    resp = client.patch(
        f"/api/admin/users/{uid}",
        json={"display_name": "新名", "password": "password2"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "新名"

    # 再 GET 详情
    detail = client.get(f"/api/admin/users/{uid}").json()
    assert detail["display_name"] == "新名"
    assert "password_hash" not in detail


def test_delete_user_soft_deactivates(client: TestClient):
    """DELETE 是软删（is_active=False），记录仍在。"""
    create = client.post("/api/admin/users", json={"name": "dave", "role": "member"}).json()
    uid = create["id"]

    resp = client.delete(f"/api/admin/users/{uid}")
    assert resp.status_code == 204

    detail = client.get(f"/api/admin/users/{uid}").json()
    assert detail["is_active"] is False  # 停用而非删除


def test_delete_admin_user_returns_400(client: TestClient):
    """删除 admin 账号 → 400（禁删，防止失去管理员）。"""
    admin_id = _seed_admin_id(client)
    resp = client.delete(f"/api/admin/users/{admin_id}")
    assert resp.status_code == 400


def test_get_missing_user_returns_404(client: TestClient):
    """不存在的用户 id → 404。"""
    resp = client.get("/api/admin/users/99999")
    assert resp.status_code == 404


def test_patch_user_role_validates_existence(client: TestClient):
    """PATCH 改 role 到不存在的角色 → 404；改成存在的角色 → 200。"""
    create = client.post("/api/admin/users", json={"name": "erin", "role": "member"}).json()
    uid = create["id"]

    # member 由 seed 创建，存在
    ok = client.patch(f"/api/admin/users/{uid}", json={"role": "member"})
    assert ok.status_code == 200

    # ghost 不存在
    bad = client.patch(f"/api/admin/users/{uid}", json={"role": "ghost"})
    assert bad.status_code == 404


# ============ 角色 ============


def test_create_role_success(client: TestClient):
    """创建角色 → 201，列表能看到。"""
    resp = client.post(
        "/api/admin/roles",
        json={
            "name": "sales",
            "description": "销售",
            "config": {
                "allowed_tools": ["kb_search"],
                "knowledge_scope_tags": ["sales", "public"],
                "level": 1,
            },
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "sales"
    assert body["config"]["allowed_tools"] == ["kb_search"]
    assert body["config"]["level"] == 1

    names = [r["name"] for r in client.get("/api/admin/roles").json()]
    assert "sales" in names


def test_create_reserved_role_admin_returns_400(client: TestClient):
    """name='admin' 是保留字 → 400。"""
    resp = client.post(
        "/api/admin/roles",
        json={"name": "admin", "description": "x", "config": {}},
    )
    assert resp.status_code == 400


def test_delete_role_referenced_by_user_returns_409(client: TestClient):
    """删除被用户引用的角色 → 409。"""
    # member 角色（seed），再绑一个用户到它
    client.post("/api/admin/users", json={"name": "frank", "role": "member"}).raise_for_status()
    resp = client.delete("/api/admin/roles/member")
    assert resp.status_code == 409


def test_delete_unreferenced_role_succeeds(client: TestClient):
    """无用户引用的角色可删除 → 204。"""
    client.post("/api/admin/roles", json={"name": "temp", "config": {}}).raise_for_status()
    resp = client.delete("/api/admin/roles/temp")
    assert resp.status_code == 204


# ============ 提示词 ============


def test_create_and_activate_prompt(client: TestClient):
    """创建提示词 → activate 后该版本 active=True，其他变 False。"""
    # seed 已有 v1 且 active
    v2 = client.post(
        "/api/admin/prompts",
        json={"version": 2, "label": "v2", "content": "新提示词", "is_active": False},
    )
    assert v2.status_code == 201, v2.text

    # 激活 v2
    act = client.post("/api/admin/prompts/activate", json={"version": 2})
    assert act.status_code == 200, act.text
    assert act.json()["is_active"] is True

    # 列表里 v1 应为 False，v2 为 True
    prompts = {p["version"]: p for p in client.get("/api/admin/prompts").json()}
    assert prompts[2]["is_active"] is True
    assert prompts[1]["is_active"] is False


def test_activate_missing_prompt_returns_404(client: TestClient):
    """激活不存在的版本 → 404。"""
    resp = client.post("/api/admin/prompts/activate", json={"version": 9999})
    assert resp.status_code == 404


def test_create_duplicate_prompt_version_returns_409(client: TestClient):
    """重复 version → 409（v1 已被 seed 创建）。"""
    resp = client.post(
        "/api/admin/prompts",
        json={"version": 1, "label": "dup", "content": "x"},
    )
    assert resp.status_code == 409


def test_list_prompts_descending_by_version(client: TestClient):
    """列表按 version 倒序（最新在前）。"""
    client.post("/api/admin/prompts", json={"version": 3, "content": "c3"})
    client.post("/api/admin/prompts", json={"version": 2, "content": "c2"})
    versions = [p["version"] for p in client.get("/api/admin/prompts").json()]
    assert versions == sorted(versions, reverse=True)


# ============ 审计 ============


def test_list_audit_returns_list(client: TestClient):
    """GET /audit 返回列表（可能空，或含 seed/操作过程产生的记录）。"""
    resp = client.get("/api/admin/audit")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_audit_respects_limit(client: TestClient):
    """limit 参数被尊重（<=500，>=1）。"""
    # 超出上限 → 422（pydantic 校验失败由 FastAPI 返回）
    resp_bad = client.get("/api/admin/audit", params={"limit": 501})
    assert resp_bad.status_code == 422

    resp_ok = client.get("/api/admin/audit", params={"limit": 5})
    assert resp_ok.status_code == 200
    body = resp_ok.json()
    assert len(body) <= 5
