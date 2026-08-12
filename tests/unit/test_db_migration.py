"""DB schema 兼容迁移测试（``_ensure_schema_compatibility`` 幂等补列）。

覆盖 S2 记忆层 Phase 1 的 ``usermemory`` 补列：旧形状表（无 source/confidence/status
三列）升级后补列成功、存量行默认值正确、重复执行不炸（幂等）。早期部署未接入 Alembic，
靠启动时的幂等迁移补齐新增列——本测试固化该保障。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session

from bycall.db import init_db, set_engine


@pytest.fixture
def engine(tmp_path: Path, monkeypatch):
    """建一个旧形状 ``usermemory`` 表的 SQLite 引擎（模拟 S2 之前的早期部署）。

    旧表只有 S2 之前的列（id/user_id/category/content/created_at/updated_at），
    并预置一条存量记忆——用于验证补列后默认值落到存量行上。
    """
    db_path = tmp_path / "test_migration.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

    get_settings.cache_clear()
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    set_engine(eng)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE usermemory ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER, "
                "category VARCHAR, "
                "content VARCHAR, "
                "created_at DATETIME, "
                "updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO usermemory (user_id, category, content) VALUES (1, 'fact', '存量记忆')"
            )
        )
    return eng


def test_usermemory_columns_added_idempotently(engine):
    """旧形状表补列成功、存量行默认值正确、重复执行不炸（幂等）。"""
    # init_db：create_all 跳过已存在的旧 usermemory 表，随后 _ensure_schema_compatibility 补列。
    init_db()

    columns = {c["name"] for c in inspect(engine).get_columns("usermemory")}
    assert {"source", "confidence", "status"} <= columns

    # 存量行拿到默认值：source=admin / confidence=1.0 / status=active（对齐现状）。
    with Session(engine) as session:
        row = session.execute(
            text("SELECT source, confidence, status FROM usermemory WHERE content = '存量记忆'")
        ).one()
        assert row.source == "admin"
        assert row.confidence == 1.0
        assert row.status == "active"

    # 重复 init_db：迁移幂等，不炸。
    init_db()


def test_migrated_memory_loads_via_user_memory_model(engine):
    """迁移后的存量记忆可被 UserMemory 模型正常召回（默认 admin + active → system 注入）。"""
    init_db()
    from bycall.agent.session_store import load_user_memory

    with Session(engine) as session:
        mems = load_user_memory(session, user_id=1)
        assert len(mems) == 1
        assert mems[0].content == "存量记忆"
        assert mems[0].source == "admin"
        assert mems[0].status == "active"
        assert mems[0].confidence == 1.0
