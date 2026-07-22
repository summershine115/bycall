"""MCPServer admin CRUD 单元测试。

验证：增删改查 + name 唯一约束 + env/headers 加密不回显 + 自动热重载接线。
用 stub manager 隔离连接逻辑（真实连接见 test_mcp_client/manager），专注控制面契约。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, set_engine


class _StubManager:
    """记录 reload/disconnect 调用，不真连——隔离 CRUD 测试。"""

    def __init__(self):
        self.reload_calls: list[int] = []
        self.disconnect_calls: list[int] = []

    def reload(self, session, server_id):
        self.reload_calls.append(server_id)
        return True, "stub ok"

    def disconnect(self, server_id):
        self.disconnect_calls.append(server_id)
        return True

    def status(self):
        return []


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    stub = _StubManager()
    monkeypatch.setattr("bycall.mcp.manager.get_mcp_manager", lambda: stub)

    app = FastAPI()
    app.include_router(router)
    # 把 stub 挂到 TestClient 上供断言。
    tc = TestClient(app)
    tc._stub = stub  # type: ignore[attr-defined]
    return tc


def test_create_then_list_masks_secrets(client):
    """创建 → 201，列表可见，env/headers 不回显明文（只标 has_*）。"""
    resp = client.post(
        "/api/admin/mcp-servers",
        json={
            "name": "sales",
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-sales"],
            "env": {"API_KEY": "super-secret"},
            "headers": {},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "sales"
    assert body["has_env"] is True
    assert body["has_headers"] is False
    assert "env" not in body and "headers" not in body  # 绝不回显
    assert body["args"] == ["mcp-server-sales"]

    # 列表也脱敏。
    listing = client.get("/api/admin/mcp-servers").json()
    assert len(listing) == 1
    assert listing[0]["has_env"] is True


def test_create_duplicate_name_409(client):
    """name 唯一约束 → 重复 409。"""
    client.post("/api/admin/mcp-servers", json={"name": "x", "transport": "stdio"})
    resp = client.post("/api/admin/mcp-servers", json={"name": "x", "transport": "stdio"})
    assert resp.status_code == 409


def test_update_env_clears_when_empty_dict(client):
    """env 传 {} = 清空（has_env → False）；不传 = 不改。"""
    sid = client.post(
        "/api/admin/mcp-servers",
        json={"name": "s", "transport": "stdio", "env": {"K": "v"}},
    ).json()["id"]
    # 清空 env
    resp = client.put(f"/api/admin/mcp-servers/{sid}", json={"env": {}})
    assert resp.json()["has_env"] is False
    # 不传 env → 不改（仍 False）
    resp = client.put(f"/api/admin/mcp-servers/{sid}", json={"description": "new"})
    assert resp.json()["has_env"] is False
    assert resp.json()["description"] == "new"


def test_update_triggers_reload(client):
    """更新启用的 server 自动调 reload。"""
    sid = client.post("/api/admin/mcp-servers", json={"name": "s", "transport": "stdio"}).json()[
        "id"
    ]
    client._stub.reload_calls.clear()  # type: ignore[attr-defined]
    client.put(f"/api/admin/mcp-servers/{sid}", json={"description": "changed"})
    assert sid in client._stub.reload_calls  # type: ignore[attr-defined]


def test_delete_disconnects(client):
    """删除调 disconnect。"""
    sid = client.post("/api/admin/mcp-servers", json={"name": "s", "transport": "stdio"}).json()[
        "id"
    ]
    resp = client.delete(f"/api/admin/mcp-servers/{sid}")
    assert resp.status_code == 204
    assert sid in client._stub.disconnect_calls  # type: ignore[attr-defined]
    assert client.get("/api/admin/mcp-servers").json() == []


def test_get_missing_404(client):
    assert client.get("/api/admin/mcp-servers/9999").status_code == 404
