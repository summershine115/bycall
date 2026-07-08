"""API 健康检查集成测试。

用 FastAPI TestClient（无需起真实 server，但走完整 ASGI 协议栈）。
本组测试不需要 postgres/redis，所以默认参与 ``make test``（不标 integration marker）。
未来需要外部服务的测试再标 ``@pytest.mark.integration``。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bycall import __version__
from bycall.api.app import create_app


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


def test_unknown_route_returns_404():
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/does-not-exist")
    assert resp.status_code == 404
