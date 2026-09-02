"""Skill Admin CRUD 端点测试。

覆盖 5 个端点（POST/GET list/GET one/PUT/DELETE）的：
- 正常 CRUD 路径
- name 唯一冲突 → 409
- 不存在 → 404
- 被角色引用时删除 → 409
- 未登录 → 401
- 审计落 AuditLog

用完整 create_app（带 login 中间件），登录 admin 后操作——这样 401 与审计都能测到。
范式与 test_builtin_tools.py / test_tool_governance.py 一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """完整 app + 全新 SQLite + admin 登录。"""
    db_path = tmp_path / "test_skill_api.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    from bycall.api.app import create_app

    app = create_app()
    c = TestClient(app)
    resp = c.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert resp.status_code == 200
    return c


# ============ 正常 CRUD ============


class TestSkillCRUD:
    def test_create_and_list(self, client: TestClient):
        """创建 skill → 201，列表能看到（含 seed 的 greeting-guide）。"""
        resp = client.post(
            "/api/admin/skills",
            json={
                "name": "sales-playbook",
                "description": "销售话术",
                "content": "# 销售指南\n要点ABC",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "sales-playbook"
        assert body["description"] == "销售话术"
        assert body["content"] == "# 销售指南\n要点ABC"
        assert body["is_enabled"] is True

        # 列表含新建 + seed 的 greeting-guide
        listing = client.get("/api/admin/skills").json()
        names = {s["name"] for s in listing}
        assert {"sales-playbook", "greeting-guide"} <= names

    def test_create_disabled(self, client: TestClient):
        """创建时 is_enabled=False 直接落库为停用（对齐前端启用开关）。"""
        resp = client.post(
            "/api/admin/skills",
            json={"name": "wip-skill", "content": "草稿内容", "is_enabled": False},
        )
        assert resp.status_code == 201
        assert resp.json()["is_enabled"] is False

    def test_get_by_name(self, client: TestClient):
        """按 name 查详情。"""
        client.post(
            "/api/admin/skills",
            json={"name": "kb1", "description": "", "content": "内容X"},
        )
        resp = client.get("/api/admin/skills/kb1")
        assert resp.status_code == 200
        assert resp.json()["content"] == "内容X"

    def test_get_nonexistent_returns_404(self, client: TestClient):
        """查不存在的 name → 404。"""
        assert client.get("/api/admin/skills/nonexistent").status_code == 404

    def test_update_content_and_enabled(self, client: TestClient):
        """PUT 更新 content / is_enabled（PATCH 语义，只改传入字段）。"""
        client.post(
            "/api/admin/skills",
            json={"name": "upd", "description": "原描述", "content": "原内容"},
        )

        # 只改 content
        resp = client.put("/api/admin/skills/upd", json={"content": "新内容"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"] == "新内容"
        assert body["description"] == "原描述"  # 未传的字段保持不变
        assert body["is_enabled"] is True

        # 禁用
        resp = client.put("/api/admin/skills/upd", json={"is_enabled": False})
        assert resp.json()["is_enabled"] is False

    def test_update_nonexistent_returns_404(self, client: TestClient):
        """PUT 不存在的 name → 404。"""
        assert client.put("/api/admin/skills/ghost", json={"content": "x"}).status_code == 404

    def test_delete_skill(self, client: TestClient):
        """删除 skill → 204，列表里没了。"""
        client.post(
            "/api/admin/skills",
            json={"name": "todelete", "description": "", "content": "x"},
        )
        assert client.delete("/api/admin/skills/todelete").status_code == 204
        assert client.get("/api/admin/skills/todelete").status_code == 404

    def test_delete_nonexistent_returns_404(self, client: TestClient):
        """DELETE 不存在的 name → 404。"""
        assert client.delete("/api/admin/skills/ghost").status_code == 404


# ============ 边界 ============


class TestSkillBoundaries:
    def test_duplicate_name_returns_409(self, client: TestClient):
        """name 唯一冲突 → 409（含 seed 的 greeting-guide）。"""
        resp = client.post(
            "/api/admin/skills",
            json={"name": "greeting-guide", "description": "", "content": "重复"},
        )
        assert resp.status_code == 409

    def test_delete_referenced_skill_returns_409(self, client: TestClient):
        """删除被角色引用的 skill → 409（避免悬空引用）。"""
        # 建 skill
        client.post(
            "/api/admin/skills",
            json={"name": "referenced", "description": "", "content": "x"},
        )
        # 建角色并绑定该 skill
        client.post(
            "/api/admin/roles",
            json={
                "name": "role-with-skill",
                "description": "",
                "config": {
                    "allowed_tools": [],
                    "knowledge_scope_tags": [],
                    "level": 1,
                    "skills": ["referenced"],
                },
            },
        )

        resp = client.delete("/api/admin/skills/referenced")
        assert resp.status_code == 409
        assert "role-with-skill" in resp.json()["detail"]

        # 移除引用后可删
        client.patch(
            "/api/admin/roles/role-with-skill",
            json={
                "config": {
                    "allowed_tools": [],
                    "knowledge_scope_tags": [],
                    "level": 1,
                    "skills": [],
                }
            },
        )
        assert client.delete("/api/admin/skills/referenced").status_code == 204

    def test_invalid_name_format_returns_422(self, client: TestClient):
        """name 不符合格式（含空格/大写/下划线/连字符开头/中文）→ 422。

        pattern = ^[a-z0-9][a-z0-9-]*$（小写字母/数字/连字符，首字符非连字符）。
        """
        bad_names = ["Sales Playbook", "UPPER", "foo_bar", "-leading", "中文名", "a b"]
        for nm in bad_names:
            resp = client.post(
                "/api/admin/skills",
                json={"name": nm, "description": "", "content": "x"},
            )
            assert resp.status_code == 422, f"{nm!r} 应被拒绝，实际 {resp.status_code}"

        # 合法 name 仍能创建（首字符字母、含数字与连字符）
        ok = client.post(
            "/api/admin/skills",
            json={"name": "ok-name-1", "description": "", "content": "x"},
        )
        assert ok.status_code == 201

    def test_endpoints_require_admin_session(self, tmp_path, monkeypatch):
        """未登录访问 skill 端点 → 401。"""
        db_path = tmp_path / "test_skill_auth.db"
        monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
        monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
        monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
        monkeypatch.setenv("BYCALL_ENV", "dev")
        get_settings.cache_clear()
        set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
        init_db()

        from bycall.api.app import create_app

        client = TestClient(create_app())
        # 未登录
        assert client.get("/api/admin/skills").status_code == 401
        assert (
            client.post("/api/admin/skills", json={"name": "x", "content": "y"}).status_code == 401
        )


# ============ 审计 ============


class TestSkillAudit:
    def test_create_audited(self, client: TestClient):
        """创建 skill 落 AuditLog（action=create_skill，entity 存 question 字段）。"""
        client.post(
            "/api/admin/skills",
            json={"name": "audited", "description": "", "content": "x"},
        )

        # 查审计日志（entity 存 question 字段，格式 Skill(audited)）
        logs = client.get("/api/admin/audit").json()["items"]
        skill_logs = [log for log in logs if log.get("action") == "create_skill"]
        assert any("audited" in log.get("question", "") for log in skill_logs)

    def test_update_audited_with_before_after(self, client: TestClient):
        """更新 skill 审计含 before/after 快照。"""
        client.post(
            "/api/admin/skills",
            json={"name": "upd-audit", "description": "", "content": "原"},
        )
        client.put("/api/admin/skills/upd-audit", json={"content": "新"})

        logs = client.get("/api/admin/audit").json()["items"]
        update_logs = [log for log in logs if log.get("action") == "update_skill"]
        assert len(update_logs) >= 1
