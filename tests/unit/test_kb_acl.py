"""T1.2 KB 级 ACL（visible_kb_ids）数据面过滤测试。

验证 ``runner._retrieve_across_kbs`` 检索前按 ``cap.visible_kb_ids`` 过滤 KB——
角色不可见的 KB 不进入检索（红线 #1 的 KB 级形态）。
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine, select

from bycall.config import get_settings
from bycall.governance.capability import compute_capability_set
from bycall.governance.models import KnowledgeBase, Role, RoleConfig, User


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


def test_retrieve_filters_kbs_by_visible_ids(db_session, monkeypatch):
    """非 admin 用户检索只命中 visible_kb_ids 内的 KB。"""
    db_session.add(Role(name="sales", config=RoleConfig(knowledge_scope_tags=["sales"], level=1)))
    db_session.add(
        KnowledgeBase(
            name="销售KB",
            description="",
            embedding_model_id=1,
            collection_name="kb_sales",
            visible_to_roles=["sales"],
        )
    )
    db_session.add(
        KnowledgeBase(
            name="HR机密KB",
            description="",
            embedding_model_id=1,
            collection_name="kb_hr",
            visible_to_roles=["hr"],
        )
    )
    db_session.add(User(name="alice", role="sales", is_active=True))
    db_session.flush()

    user = db_session.exec(select(User).where(User.name == "alice")).first()
    cap = compute_capability_set(db_session, user)

    # 用户角色 sales → 只对 kb_sales 可见（kb_hr 仅 hr 可见）
    captured: list[list[tuple]] = []

    def fake_retrieve(
        session,
        config,
        kb_specs,
        question,
        knowledge_scope_tags=None,
        topk=5,
        level=0,
        is_admin=False,
        **kwargs,
    ):
        captured.append(list(kb_specs))
        return []

    from bycall.agent import runner as runner_module

    monkeypatch.setattr("bycall.rag.retrieve.retrieve_multi_kb", fake_retrieve)
    result = runner_module._retrieve_across_kbs(db_session, "q", cap)

    assert result == []
    assert len(captured) == 1
    kb_specs = captured[0]
    collection_names = {spec[1] for spec in kb_specs}
    assert "kb_sales" in collection_names
    assert "kb_hr" not in collection_names
