"""API 健康检查集成测试。

用 FastAPI TestClient（无需起真实 server，但走完整 ASGI 协议栈）。
``create_app()`` 现在会 init_db，所以需要一个 SQLite 引擎 fixture（否则走默认 Postgres DSN）。
本组测试不需要 postgres/redis，所以默认参与 ``make test``（不标 integration marker）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall import __version__
from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, set_engine


@pytest.fixture(autouse=True)
def sqlite_env(tmp_path, monkeypatch):
    """每个 health 测试用独立 SQLite + init_db，避免走 Postgres。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_file}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False}))
    init_db()
    yield
    get_settings.cache_clear()


def test_health_returns_ok():
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_reports_version_and_backends():
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    # 默认后端选型（M0）
    assert body["llm_provider"] in ("openai", "glm")
    assert body["vector_store_backend"] == "zvec"


def test_ready_does_not_leak_secrets():
    """/ready 绝不能回显 api_key / DSN 等敏感字段。"""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/ready")
    body_text = resp.text.lower()
    for forbidden in ("api_key", "apikey", "secret", "password", "dsn", "sk-"):
        assert forbidden not in body_text, f"/ready 泄漏敏感字段: {forbidden}"


def test_docs_endpoint_available():
    """M0 保留 /docs（Swagger UI）便于联调；/redoc 关闭。"""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/docs")
    assert resp.status_code == 200


def test_unknown_api_route_returns_404():
    """未知 API 路径返回 404（不被 SPA fallback 吞掉）。

    注意：SPA 挂载后，非 API 的未知路径会 fallback 到 index.html（history 模式路由），
    这是正确行为。这里只验证 API 前缀下的未知路径仍 404。
    """
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
