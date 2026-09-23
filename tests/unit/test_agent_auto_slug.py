"""数字员工内部标识（slug）自动生成——管理员无需指定（ADR-0013 修订）。

slug 不是寻址键（控制面/数据面一律按 ``id`` 寻址），只作审计与排障标识。控制面因此
把它对管理员隐藏：创建时只填名称，标识由后端按显示名生成（ASCII 化失败则退化随机后缀）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.governance.models import Agent


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """完整 app（含会话守卫）+ 临时 SQLite。"""
    db_path = tmp_path / "test_slug.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    from bycall.api.app import create_app

    return TestClient(create_app())


def _login(c: TestClient) -> None:
    c.post("/api/login", data={"name": "admin", "password": "admin123"})


def _names() -> list[str]:
    """库里全部实例的内部标识（新开会话读，避免与 app 会话纠缠）。"""
    engine = create_engine(get_settings().postgres.dsn, connect_args={"check_same_thread": False})
    with Session(engine) as s:
        return sorted(a.name for a in s.exec(select(Agent)).all())


def test_create_without_name_autogenerates_ascii_slug(client: TestClient):
    """不传 name → 由显示名 ASCII 化生成，管理员不必知道 slug 的存在。"""
    _login(client)
    resp = client.post("/api/admin/agents", json={"display_name": "Sales Assistant"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "sales-assistant"
    assert _names() == ["sales-assistant"]


def test_create_without_name_chinese_display_name(client: TestClient):
    """纯中文显示名 ASCII 化后为空 → 退化为 agent / agent-XXXX，仍然合法且唯一。"""
    _login(client)
    first = client.post("/api/admin/agents", json={"display_name": "销售助手"})
    second = client.post("/api/admin/agents", json={"display_name": "库存助手"})
    assert first.status_code == second.status_code == 201
    slug_re = re.compile(r"^agent(-[0-9a-f]{4})?$")
    assert slug_re.match(first.json()["name"])
    assert slug_re.match(second.json()["name"])
    # 同名/同前缀也不会撞车
    assert first.json()["name"] != second.json()["name"]
    assert len(_names()) == 2


def test_create_repeated_display_name_stays_unique(client: TestClient):
    """两次同名创建 → 两个不同标识（用户侧看到的是同一个名称，系统自己区分）。"""
    _login(client)
    a = client.post("/api/admin/agents", json={"display_name": "客服助手"})
    b = client.post("/api/admin/agents", json={"display_name": "客服助手"})
    assert a.status_code == b.status_code == 201
    assert a.json()["name"] != b.json()["name"]


def test_explicit_name_still_supported_and_unique(client: TestClient):
    """显式传入 name 仍然生效（兼容既有 API 调用），重复即 409。"""
    _login(client)
    first = client.post("/api/admin/agents", json={"name": "sales-assistant"})
    assert first.status_code == 201, first.text
    assert first.json()["name"] == "sales-assistant"

    dup = client.post("/api/admin/agents", json={"name": "sales-assistant"})
    assert dup.status_code == 409

    # 非法显式 name 仍被 schema 拒绝（不静默改写）
    bad = client.post("/api/admin/agents", json={"name": "Bad Slug"})
    assert bad.status_code == 422
