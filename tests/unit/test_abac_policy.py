"""T1.1 密级/职级 ABAC：统一 policy 接口 + level 召回前预过滤。

覆盖：
- ``governance.policy.build_acl_filters`` 把主体属性翻译成过滤条件（admin/通配/普通）。
- 端到端：文档 level → chunk ACL → zvec 字段 → 检索 ``level <= user_level`` 过滤。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bycall.config import get_settings
from bycall.governance.capability import CapabilitySet
from bycall.governance.policy import PolicySubject, build_acl_filters, subject_from_capability
from bycall.llm.base import EmbedModel
from bycall.rag.ingest import IngestPipeline
from bycall.rag.retrieve import retrieve_context
from bycall.vector_store.factory import create_vector_store
from bycall.vector_store.models import FilterCondition, SearchRequest

DIM = 8


# ============ 1. policy 纯函数 ============


def test_admin_no_filters():
    """admin 不过滤（全可见）。"""
    s = PolicySubject(is_admin=True, knowledge_scope_tags=["*"], level=999)
    assert build_acl_filters(s) == []


def test_wildcard_no_filters():
    """通配 '*' 不过滤（兼容旧行为）。"""
    s = PolicySubject(is_admin=False, knowledge_scope_tags=["*"], level=0)
    assert build_acl_filters(s) == []


def test_regular_user_gets_source_and_level_filters():
    """普通用户 → source IN + level <= user_level。"""
    s = PolicySubject(is_admin=False, knowledge_scope_tags=["sales", "public"], level=2)
    filters = build_acl_filters(s)
    by_field = {f.field: f for f in filters}
    assert by_field["source"].op == "in"
    assert by_field["source"].values == ["sales", "public"]
    assert by_field["level"].op == "lte"
    assert by_field["level"].value == 2


def test_level_zero_still_emits_filter():
    """level=0 也发条件：机密文档（level>=1）对普通用户 fail-closed 过滤。"""
    s = PolicySubject(is_admin=False, knowledge_scope_tags=["public"], level=0)
    filters = build_acl_filters(s)
    assert any(f.field == "level" and f.op == "lte" and f.value == 0 for f in filters)


def test_subject_from_capability():
    """从 CapabilitySet 裁剪出 ABAC 判定所需字段。"""
    cap = CapabilitySet(user_id=1, user_name="alice", role="sales", knowledge_scope_tags=["sales"], level=3)
    s = subject_from_capability(cap)
    assert s.is_admin is False
    assert s.knowledge_scope_tags == ["sales"]
    assert s.level == 3


def test_filtercondition_lte_matches():
    """FilterCondition.matches 支持范围比较（lte）。"""
    from types import SimpleNamespace

    assert FilterCondition(field="level", op="lte", value=0).matches(SimpleNamespace(level=0))
    assert not FilterCondition(field="level", op="lte", value=0).matches(SimpleNamespace(level=1))
    assert FilterCondition(field="level", op="lte", value=5).matches(SimpleNamespace(level=5))


# ============ 2. 端到端 level 预过滤 ============


def _make_zero_embed() -> MagicMock:
    m = MagicMock(spec=EmbedModel)
    m.dimension = DIM
    m.embed.side_effect = lambda texts: [[0.0] * DIM for _ in texts]
    return m


@pytest.fixture
def abac_store(tmp_path: Path, monkeypatch):
    """zvec 临时目录 + 两条同 source、不同密级的文档（公开 level=0 / 机密 level=5）。"""
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "8")
    get_settings.cache_clear()
    store = create_vector_store(get_settings().vector_store)
    pipeline = IngestPipeline(store, _make_zero_embed(), collection_name="knowledge")
    pipeline.ingest_text("公开报销流程 public guide", source="public", level=0)
    pipeline.ingest_text("机密并购计划 secret confidential", source="public", level=5)
    return store


def test_level_abac_filters_confidential_chunk(abac_store):
    """level=0 用户召回不到 level=5 的机密文档；level=5 用户能。"""
    # 低职级：查询机密关键词，机密 chunk（level=5）被 level <= 0 过滤掉。
    low = retrieve_context(abac_store, "secret", knowledge_scope_tags=["public"], level=0)
    assert "secret" not in " ".join(h.text for h in low)

    # 高职级：level <= 5 放行，能召回机密 chunk。
    high = retrieve_context(abac_store, "secret", knowledge_scope_tags=["public"], level=5)
    assert any("secret" in h.text for h in high)


def test_level_lte_filter_direct(abac_store):
    """zvec 直接按 level <= N 过滤（范围比较 op 落地）。"""
    low = abac_store.search(SearchRequest(full_text="secret", filters=[FilterCondition(field="level", op="lte", value=0)]))
    assert low == []
    high = abac_store.search(SearchRequest(full_text="secret", filters=[FilterCondition(field="level", op="lte", value=5)]))
    assert any("secret" in h.text for h in high)