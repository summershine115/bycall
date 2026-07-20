"""资源/费用控制测试（P1-4：输入长度/上传大小无限制）。

验证：
- /chat 问题超长 → 400
- 文件上传超 10MB → 413
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    """挂载完整 app 的 TestClient。"""
    db_path = tmp_path / "test_limits.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    from bycall.api.app import create_app

    app = create_app()
    return TestClient(app)


def test_chat_question_too_long_rejected(app_client: TestClient):
    """/chat 问题超 4000 字符 → 400（不进治理链路）。"""
    # 先登录
    app_client.post("/api/login", data={"name": "admin", "password": "admin123"})

    long_question = "x" * 4001
    resp = app_client.post("/chat", json={"question": long_question})
    assert resp.status_code == 400
    assert "过长" in resp.json()["detail"]


def test_chat_question_at_limit_accepted(app_client: TestClient):
    """/chat 问题恰 4000 字符 → 不是 400（边界值，不因长度被拒）。

    可能因其他原因（未激活模型等）返回非 200，但不应是 400 长度错误。
    """
    app_client.post("/api/login", data={"name": "admin", "password": "admin123"})

    resp = app_client.post("/chat", json={"question": "x" * 4000})
    # 不是 400（长度校验通过），可能是 503（无模型）或其他，但不是长度拒绝
    assert resp.status_code != 400 or "过长" not in resp.json().get("detail", "")


def test_upload_oversize_rejected(app_client: TestClient):
    """上传 >10MB → 413。"""
    app_client.post("/api/login", data={"name": "admin", "password": "admin123"})

    # 先建 KB（需要一个 embedding model）
    mid = app_client.post(
        "/api/admin/models",
        json={"name": "m", "model_type": "embedding", "embed_dimension": 16, "is_active": True},
    ).json()["id"]
    kb_id = app_client.post(
        "/api/admin/knowledge-bases",
        json={"name": "kb", "description": "", "embedding_model_id": mid},
    ).json()["id"]

    # 11MB 文件
    big_content = b"x" * (11 * 1024 * 1024)
    resp = app_client.post(
        f"/api/admin/knowledge-bases/{kb_id}/upload",
        params={"source": "public", "title": "big"},
        files={"file": ("big.txt", big_content, "text/plain")},
    )
    assert resp.status_code == 413
    assert "过大" in resp.json()["detail"]
