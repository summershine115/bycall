"""限流测试（P1-4：登录/聊天无限流可被暴力破解/费用 DoS）。

测试内存模式（无 Redis 时的降级路径），CI 无 Redis 也能跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.governance.ratelimit import RateLimiter, get_limiter


def test_memory_mode_allows_within_limit():
    """内存模式：limit 内的请求都允许。"""
    limiter = RateLimiter()  # 无 Redis → 内存模式
    assert limiter._mode == "memory"

    for _ in range(5):
        assert limiter.allow("test_key", limit=5, window=60) is True


def test_memory_mode_blocks_over_limit():
    """内存模式：第 limit+1 次被拒。"""
    limiter = RateLimiter()

    for _ in range(3):
        assert limiter.allow("blocked_key", limit=3, window=60) is True
    # 第 4 次超限
    assert limiter.allow("blocked_key", limit=3, window=60) is False


def test_memory_mode_different_keys_independent():
    """内存模式：不同 key 的计数互不影响。"""
    limiter = RateLimiter()

    for _ in range(3):
        limiter.allow("key_a", limit=3, window=60)
    # key_b 仍有全部额度
    assert limiter.allow("key_b", limit=3, window=60) is True
    assert limiter.allow("key_b", limit=3, window=60) is True
    assert limiter.allow("key_b", limit=3, window=60) is True
    assert limiter.allow("key_b", limit=3, window=60) is False


def test_get_limiter_singleton():
    """get_limiter 返回单例。"""
    a = get_limiter()
    b = get_limiter()
    assert a is b


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch):
    """完整 app TestClient（登录限流集成测试用）。"""
    db_path = tmp_path / "test_rl.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    from bycall.api.app import create_app

    app = create_app()
    return TestClient(app)


def test_login_rate_limit_integration(app_client: TestClient):
    """集成：连续 5 次错误登录后第 6 次 → 429。"""
    for _ in range(5):
        resp = app_client.post("/api/login", data={"name": "admin", "password": "wrong"})
        assert resp.status_code == 401

    # 第 6 次 → 429（即使密码正确也被限流）
    resp = app_client.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert resp.status_code == 429
