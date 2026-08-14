"""T1.5 会话键多租户预留（tenant_id / agent_id）测试。

验证 Conversation 预留列默认值正确（单组织部署，ADR-0004）+ 列可承载跨租户隔离。
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine, select

from bycall.config import get_settings
from bycall.governance.models import Conversation


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, session_scope, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()
    with session_scope() as s:
        yield s


def test_conversation_tenant_agent_defaults(db_session):
    """Conversation 默认 tenant_id=default、agent_id=None（单组织部署，ADR-0004）。"""
    conv = Conversation(user_id=1, channel="wecom", session_key="wecom:1")
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    assert conv.tenant_id == "default"
    assert conv.agent_id is None


def test_conversation_tenant_isolation_columns(db_session):
    """不同 tenant_id 的同 (user_id, channel) 会话可共存（隔离列已就位）。"""
    a = Conversation(user_id=1, channel="wecom", session_key="a:wecom:1", tenant_id="acme")
    b = Conversation(user_id=1, channel="wecom", session_key="b:wecom:1", tenant_id="globex")
    db_session.add_all([a, b])
    db_session.commit()
    rows = db_session.exec(select(Conversation).where(Conversation.user_id == 1)).all()
    assert {r.tenant_id for r in rows} == {"acme", "globex"}
