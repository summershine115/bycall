"""浏览器安全边界：跨站写请求拒绝，响应包含基础安全头。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import set_engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "security-middleware.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))

    from bycall.api.app import create_app

    app = create_app()
    c = TestClient(app)
    login = c.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert login.status_code == 200
    return c


def test_cross_origin_authenticated_write_rejected(client):
    response = client.post(
        "/api/admin/prompts/activate",
        json={"version": 1},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_same_origin_authenticated_write_allowed(client):
    response = client.post(
        "/api/admin/prompts/activate",
        json={"version": 1},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200


def test_security_headers_present(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
