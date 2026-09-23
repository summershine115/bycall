"""存量库升级：淘汰「默认员工」遗留列的迁移（ADR-0013 修订）。

回归的是一次真实事故：旧模型把 ``agent.is_default`` 建为 ``NOT NULL`` 且**没有数据库默认值**
（Python 侧 default 不会写进 DDL），模型去掉该字段后 INSERT 不再提供这一列 → 存量库新建实例
必然 ``NOT NULL constraint failed``，而且被 ``except IntegrityError`` 分支误报成「数字员工已存在」。

本用例用**旧模型当时的真实 DDL** 建表（含 ``is_default BOOLEAN NOT NULL`` 与 ``ix_agent_is_default``
索引），再跑 ``init_db``，验证：列被幂等清理、既有数据不丢、控制面创建实例重新可用。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import create_engine

from bycall.config import get_settings

# 旧模型（含 is_default 字段）建出的表 DDL——取自真实存量库 sqlite_master。
LEGACY_AGENT_DDL = """
CREATE TABLE agent (
    id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    display_name VARCHAR NOT NULL,
    description VARCHAR NOT NULL,
    avatar_url VARCHAR NOT NULL,
    system_prompt_version INTEGER,
    model_id INTEGER,
    skills VARCHAR,
    knowledge_scope_tags VARCHAR,
    allowed_tools VARCHAR,
    visible_roles VARCHAR,
    channel VARCHAR NOT NULL,
    im_config_encrypted VARCHAR NOT NULL,
    approval_policy VARCHAR,
    is_enabled BOOLEAN NOT NULL,
    is_default BOOLEAN NOT NULL,
    created_at DATETIME,
    updated_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT uq_agent_name UNIQUE (name)
)
"""


@pytest.fixture
def legacy_engine(tmp_path: Path, monkeypatch):
    """造一个「升级前」的库：旧 agent 表 + 一条既有实例 + 历史索引。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/legacy.db")
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{tmp_path}/legacy.db", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(LEGACY_AGENT_DDL))
        conn.execute(text("CREATE INDEX ix_agent_is_default ON agent (is_default)"))
        conn.execute(
            text(
                "INSERT INTO agent (name, display_name, description, avatar_url, channel,"
                " im_config_encrypted, is_enabled, is_default)"
                " VALUES ('legacy-kept', '老实例', '', '', 'api', '', 1, 0)"
            )
        )
    from bycall.db import set_engine

    set_engine(engine)
    return engine


def _columns(engine) -> set[str]:
    with engine.begin() as conn:
        return {row[1] for row in conn.execute(text("PRAGMA table_info(agent)"))}


def test_init_db_drops_legacy_column_and_keeps_data(legacy_engine):
    """迁移后：is_default 列消失，既有实例原样保留。"""
    from bycall.db import init_db

    assert "is_default" in _columns(legacy_engine)

    init_db()

    assert "is_default" not in _columns(legacy_engine)
    with legacy_engine.begin() as conn:
        names = [row[0] for row in conn.execute(text("SELECT name FROM agent ORDER BY id"))]
    assert names == ["legacy-kept"]

    # 幂等：再跑一次不报错
    init_db()
    assert "is_default" not in _columns(legacy_engine)


def test_create_agent_works_on_upgraded_legacy_db(legacy_engine):
    """回归：迁移后控制面创建实例必须成功（修复前这里是 NOT NULL 失败 → 伪 409「已存在」）。"""
    from fastapi.testclient import TestClient

    from bycall.api.app import create_app
    from bycall.db import init_db

    init_db()
    client = TestClient(create_app())
    client.post("/api/login", data={"name": "admin", "password": "admin123"})

    resp = client.post("/api/admin/agents", json={"display_name": "销售助手"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["display_name"] == "销售助手"
