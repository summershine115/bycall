"""策略引擎单测：验证「用户+角色 → 能力集」翻译规则。

覆盖：
- admin 内置角色（通配 + 最高职级）
- 普通用户按 Role.config 收窄
- 角色未定义 / 用户被禁用 → PermissionError
- 激活系统提示词取值与兜底
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine, select

import bycall.governance.capability as cap_module
from bycall.config import get_settings
from bycall.governance.capability import (
    CapabilitySet,
    build_knowledge_scope_text,
    clear_capability_cache,
    compute_capability_set,
    get_active_system_prompt,
)
from bycall.governance.models import KnowledgeBase, Role, RoleConfig, SystemPrompt, User


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含 admin/member/默认提示词）。

    每个用例独立文件库，互不污染；用例结束 session_scope 自动提交关闭。
    """
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, session_scope, set_engine

    set_engine(
        create_engine(
            f"sqlite:///{tmp_path}/test.db",
            connect_args={"check_same_thread": False},
        )
    )
    init_db()
    with session_scope() as s:
        yield s


def _make_user(
    session,
    name: str,
    role: str,
    is_active: bool = True,
) -> User:
    """插入一个 User 并刷新拿到 id，返回可用于 compute 的实例。"""
    user = User(
        name=name,
        display_name=name,
        role=role,
        is_active=is_active,
    )
    session.add(user)
    session.flush()  # 拿到自增 id
    return user


# ============ admin 内置角色 ============


def test_admin_user_gets_full_capability(db_session):
    """admin 内置角色：is_admin=True、通配工具/知识域、最高职级 999。"""
    admin = db_session.exec(User.select_by_name("admin")).first()
    assert admin is not None

    cap = compute_capability_set(db_session, admin)

    assert isinstance(cap, CapabilitySet)
    assert cap.is_admin is True
    assert cap.role == "admin"
    assert cap.allowed_tools == ["*"]
    assert cap.knowledge_scope_tags == ["*"]
    assert cap.level == 999
    assert cap.user_id == admin.id
    assert cap.user_name == "admin"


# ============ 普通用户按 Role.config 收窄 ============


def test_normal_user_reflects_role_config(db_session):
    """普通用户能力集严格反映其 Role.config（工具/知识域/职级）。"""
    sales_cfg = RoleConfig(
        allowed_tools=["kb_search", "crm_lookup"],
        knowledge_scope_tags=["sales", "public"],
        level=3,
    )
    db_session.add(Role(name="sales", description="销售", config=sales_cfg))
    db_session.flush()

    user = _make_user(db_session, "alice", role="sales")
    cap = compute_capability_set(db_session, user)

    assert cap.is_admin is False
    assert cap.role == "sales"
    assert cap.allowed_tools == ["kb_search", "crm_lookup"]
    assert cap.knowledge_scope_tags == ["sales", "public"]
    assert cap.level == 3
    assert cap.user_id == user.id
    assert cap.user_name == "alice"


def test_default_member_role_has_empty_capability(db_session):
    """init_db 灌的默认 member 角色：空工具/空知识域/level=0（最小权限）。"""
    user = _make_user(db_session, "bob", role="member")
    cap = compute_capability_set(db_session, user)

    assert cap.is_admin is False
    assert cap.allowed_tools == []
    assert cap.knowledge_scope_tags == []
    assert cap.level == 0


def test_capability_set_returns_copy_not_shared_reference(db_session):
    """返回的 list 是独立副本，调用方改动不应回写 Role.config（值对象语义）。"""
    db_session.add(
        Role(
            name="eng",
            config=RoleConfig(allowed_tools=["kb_search"], knowledge_scope_tags=["eng"], level=2),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "carol", role="eng")

    cap = compute_capability_set(db_session, user)
    cap.allowed_tools.append("INJECTED")
    cap.knowledge_scope_tags.append("INJECTED")

    # 重新计算，确认源 Role.config 未被污染。
    cap2 = compute_capability_set(db_session, user)
    assert cap2.allowed_tools == ["kb_search"]
    assert cap2.knowledge_scope_tags == ["eng"]


# ============ 角色未定义 → PermissionError ============


def test_undefined_role_raises_permission_error(db_session):
    """role 对应 Role 记录不存在（非 admin）→ PermissionError("角色未定义: ...")。"""
    user = _make_user(db_session, "dave", role="ghost")  # 没建过 "ghost" 角色

    with pytest.raises(PermissionError, match="角色未定义: ghost"):
        compute_capability_set(db_session, user)


# ============ 用户被禁用 → PermissionError ============


def test_inactive_user_raises_permission_error(db_session):
    """is_active=False 用户无论角色都拒绝（"用户已被禁用"）。"""
    # 即便配了合法角色，禁用用户也应被拦下。
    db_session.add(
        Role(
            name="sales",
            config=RoleConfig(allowed_tools=["kb_search"], knowledge_scope_tags=["sales"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "eve", role="sales", is_active=False)

    with pytest.raises(PermissionError, match="用户已被禁用"):
        compute_capability_set(db_session, user)


def test_inactive_admin_still_rejected(db_session):
    """禁用优先级高于 admin 身份——禁用的 admin 也应被拒绝。"""
    admin = db_session.exec(User.select_by_name("admin")).first()
    assert admin is not None
    admin.is_active = False
    db_session.add(admin)
    db_session.flush()

    with pytest.raises(PermissionError, match="用户已被禁用"):
        compute_capability_set(db_session, admin)


# ============ 激活系统提示词取值 ============


def test_active_system_prompt_attached(db_session):
    """能力集 system_prompt 取当前激活的 SystemPrompt.content。"""
    user = _make_user(db_session, "frank", role="member")
    cap = compute_capability_set(db_session, user)

    expected = get_active_system_prompt(db_session)
    assert cap.system_prompt == expected
    assert cap.system_prompt != ""  # init_db 灌了默认激活提示词


def test_system_prompt_reflects_active_version_change(db_session):
    """改激活版本后，能力集 system_prompt 跟着变。"""
    # 新增 v2 并把激活从 v1 切到 v2。
    v1 = db_session.get(SystemPrompt, 1)
    assert v1 is not None
    assert v1.is_active is True
    v1.is_active = False
    db_session.add(v1)

    v2_content = "这是 v2 的新提示词，完全不同。"
    db_session.add(SystemPrompt(version=2, label="v2", content=v2_content, is_active=True))
    db_session.flush()

    user = _make_user(db_session, "grace", role="member")
    cap = compute_capability_set(db_session, user)

    assert cap.system_prompt == v2_content


# ============ get_active_system_prompt 兜底 ============


def test_get_active_system_prompt_fallback_when_none(db_session):
    """无激活版本时 get_active_system_prompt 返回空串兜底。"""
    # 把默认那条激活的关掉，全局不再有激活版本。
    v1 = db_session.get(SystemPrompt, 1)
    assert v1 is not None
    v1.is_active = False
    db_session.add(v1)
    db_session.flush()

    assert get_active_system_prompt(db_session) == ""


def test_get_active_system_prompt_returns_content(db_session):
    """有激活版本时返回其 content。"""
    v1 = db_session.get(SystemPrompt, 1)
    assert v1 is not None
    assert v1.is_active is True

    assert get_active_system_prompt(db_session) == v1.content


def test_capability_falls_back_to_empty_prompt_when_no_active(db_session):
    """无激活提示词时，能力集 system_prompt 为空串（不致错，只是不注入指令）。"""
    v1 = db_session.get(SystemPrompt, 1)
    assert v1 is not None
    v1.is_active = False
    db_session.add(v1)
    db_session.flush()

    user = _make_user(db_session, "heidi", role="member")
    cap = compute_capability_set(db_session, user)
    assert cap.system_prompt == ""


# ============ 角色 prompt 绑定（system_prompt_version） ============


def test_role_bound_prompt_overrides_global(db_session):
    """角色绑定了 system_prompt_version → 用该版本，而非全局激活版。"""
    # 新增 v2（不激活），让 sales 角色绑定它。
    v2_content = "你是销售专员，用专业话术回答客户。"
    db_session.add(
        SystemPrompt(version=2, label="sales_prompt", content=v2_content, is_active=False)
    )
    db_session.flush()

    db_session.add(
        Role(
            name="sales",
            config=RoleConfig(
                allowed_tools=["kb_search"],
                knowledge_scope_tags=["sales"],
                level=2,
                system_prompt_version=2,
            ),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "ivan", role="sales")

    cap = compute_capability_set(db_session, user)
    assert cap.system_prompt == v2_content
    # 不等于全局激活版（v1）
    assert cap.system_prompt != get_active_system_prompt(db_session)


def test_role_without_bound_prompt_uses_global(db_session):
    """角色未绑定 system_prompt_version（None）→ 用全局激活版（后向兼容）。"""
    db_session.add(
        Role(
            name="hr",
            config=RoleConfig(
                allowed_tools=["kb_search"],
                knowledge_scope_tags=["hr"],
                level=1,
                system_prompt_version=None,
            ),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "judy", role="hr")

    cap = compute_capability_set(db_session, user)
    assert cap.system_prompt == get_active_system_prompt(db_session)


def test_role_bound_prompt_deleted_falls_back_to_global(db_session):
    """角色绑定的版本已被删除 → fallback 全局激活版（不阻断，记 warning）。"""
    # sales 绑定 v99（根本不存在），应 fallback 到全局激活的 v1。
    db_session.add(
        Role(
            name="sales",
            config=RoleConfig(
                allowed_tools=["kb_search"],
                knowledge_scope_tags=["sales"],
                level=2,
                system_prompt_version=99,
            ),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "karl", role="sales")

    cap = compute_capability_set(db_session, user)
    assert cap.system_prompt == get_active_system_prompt(db_session)


def test_admin_ignores_role_bound_prompt(db_session):
    """admin 内置角色始终用全局激活版（不走 Role 绑定）。"""
    # admin 无 Role 表记录，不受 system_prompt_version 影响。
    admin = db_session.exec(User.select_by_name("admin")).first()
    assert admin is not None

    cap = compute_capability_set(db_session, admin)
    assert cap.system_prompt == get_active_system_prompt(db_session)


# ============ 可见知识域注入文本（build_knowledge_scope_text） ============


_KBS = [("产品知识库", "产品规格与使用说明"), ("HR知识库", "人事制度与考勤")]


def test_knowledge_scope_text_admin_enumerates_all_kbs():
    """admin / 全通配 → 全部知识域，并逐一枚举组织内知识库（名称+描述）。"""
    text = build_knowledge_scope_text(["*"], is_admin=True, kbs=_KBS)
    assert "全部知识域" in text
    assert "管理员" in text
    assert "产品知识库：产品规格与使用说明" in text
    assert "HR知识库：人事制度与考勤" in text


def test_knowledge_scope_text_wildcard_tag_treated_as_full_access():
    """非 admin 但 tags 含通配 '*' → 同样视为全部知识域（与 admin 同义）。"""
    text = build_knowledge_scope_text(["*"], is_admin=False, kbs=_KBS)
    assert "全部知识域" in text


def test_knowledge_scope_text_normal_user_lists_tags_and_kbs():
    """普通用户 → 列出来源标签 + 组织内知识库，并要求据实回答、不要编造。"""
    text = build_knowledge_scope_text(["sales", "public"], is_admin=False, kbs=_KBS)
    assert "sales" in text and "public" in text
    assert "产品知识库" in text
    assert "不要编造" in text


def test_knowledge_scope_text_empty_returns_empty():
    """空 tags → 空串（防御性，该用户已被 runner denied_no_access 拦在 LLM 之前）。"""
    assert build_knowledge_scope_text([], is_admin=False, kbs=_KBS) == ""


def test_knowledge_scope_text_no_kbs_shows_placeholder():
    """无任何知识库 → 枚举处显示占位，不报错。"""
    text = build_knowledge_scope_text(["*"], is_admin=True, kbs=[])
    assert "暂无知识库" in text


# ============ 能力集携带 knowledge_scope_text ============


def test_admin_capability_scope_text_enumerates_kbs(db_session):
    """admin 能力集的 knowledge_scope_text 说明全部知识域并枚举组织内知识库。"""
    db_session.add(
        KnowledgeBase(
            name="产品知识库",
            description="产品规格",
            embedding_model_id=1,
            collection_name="kb_prod",
        )
    )
    db_session.flush()
    admin = db_session.exec(User.select_by_name("admin")).first()
    assert admin is not None
    cap = compute_capability_set(db_session, admin)
    assert "全部知识域" in cap.knowledge_scope_text
    assert "产品知识库" in cap.knowledge_scope_text


def test_normal_capability_scope_text_lists_role_tags(db_session):
    """普通用户能力集的 knowledge_scope_text 含来源标签 + 知识库范围。"""
    db_session.add(
        Role(
            name="sales",
            config=RoleConfig(
                allowed_tools=["kb_search"],
                knowledge_scope_tags=["sales", "public"],
                level=2,
            ),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "alice", role="sales")
    cap = compute_capability_set(db_session, user)
    assert "sales" in cap.knowledge_scope_text
    assert "public" in cap.knowledge_scope_text


def test_normal_capability_hides_role_restricted_kb_names(db_session):
    """普通用户的能力自描述只枚举其角色可见的知识库。"""
    db_session.add(
        Role(
            name="sales_limited",
            config=RoleConfig(knowledge_scope_tags=["sales"], level=1),
        )
    )
    db_session.add(
        KnowledgeBase(
            name="销售知识库",
            description="销售资料",
            embedding_model_id=1,
            collection_name="kb_sales",
            visible_to_roles=["sales_limited"],
        )
    )
    db_session.add(
        KnowledgeBase(
            name="HR机密知识库",
            description="仅 HR 可见",
            embedding_model_id=1,
            collection_name="kb_hr_secret",
            visible_to_roles=["hr"],
        )
    )
    db_session.flush()

    user = _make_user(db_session, "sales_user", role="sales_limited")
    cap = compute_capability_set(db_session, user)

    assert "销售知识库" in cap.knowledge_scope_text
    assert "HR机密知识库" not in cap.knowledge_scope_text
    assert len(cap.visible_kb_ids) == 1


# ============ 能力集缓存（M5 性能调优）============


def test_cache_hit_skips_db_queries(db_session, monkeypatch):
    """命中跳过全部 DB 查询——spy 底层计算函数，连续 3 次只算 1 次。"""
    clear_capability_cache()
    db_session.add(
        Role(
            name="eng",
            config=RoleConfig(allowed_tools=["a"], knowledge_scope_tags=["eng"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "wendy", role="eng")

    calls = {"n": 0}
    orig = cap_module._compute_capability_set_uncached

    def spy(session, user, agent=None):
        calls["n"] += 1
        return orig(session, user, agent)

    monkeypatch.setattr(cap_module, "_compute_capability_set_uncached", spy)

    compute_capability_set(db_session, user)  # miss → 算 1 次
    compute_capability_set(db_session, user)  # hit → 不算
    compute_capability_set(db_session, user)  # hit → 不算
    assert calls["n"] == 1


def test_cache_hit_returns_independent_copy(db_session):
    """命中返回 deepcopy 副本：改第一个 cap 不污染缓存，后续取仍是干净的。

    这是治理系统的硬约束——一次请求对返回能力集的改动，绝不能泄漏到下一次请求
    （否则 A 改宽自己的 allowed_tools，B 拿到的就是被污染的越权集合）。
    """
    clear_capability_cache()
    db_session.add(
        Role(
            name="eng",
            config=RoleConfig(allowed_tools=["a"], knowledge_scope_tags=["eng"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "xander", role="eng")

    cap1 = compute_capability_set(db_session, user)  # miss → 缓存
    cap2 = compute_capability_set(db_session, user)  # hit
    cap1.allowed_tools.append("INJECTED")
    cap1.knowledge_scope_tags.append("INJECTED")

    assert cap2.allowed_tools == ["a"]  # cap2 未受 cap1 改动影响
    assert cap2.knowledge_scope_tags == ["eng"]
    cap3 = compute_capability_set(db_session, user)  # 缓存内 master 未被污染
    assert cap3.allowed_tools == ["a"]
    assert cap3.knowledge_scope_tags == ["eng"]


def test_cache_invalidated_on_relevant_commit(db_session):
    """admin 改 Role.config 提交后，缓存立即失效——下次重算反映新配置。

    正确性核心：admin 收窄权限（如删工具）必须立即生效，不能让已缓存的能力集
    继续放行被收回的工具。after_commit 据相关表脏实例清空整张缓存。
    """
    clear_capability_cache()
    db_session.add(
        Role(
            name="eng",
            config=RoleConfig(allowed_tools=["a"], knowledge_scope_tags=["eng"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "yvonne", role="eng")
    cap1 = compute_capability_set(db_session, user)
    assert cap1.allowed_tools == ["a"]
    assert (user.id, None) in cap_module._CAP_CACHE

    # 改 Role.config 并提交（模拟 admin 写路径：控制面端点 commit）
    role = db_session.exec(select(Role).where(Role.name == "eng")).first()
    role.config = RoleConfig(allowed_tools=["a", "b"], knowledge_scope_tags=["eng"], level=1)
    db_session.add(role)
    db_session.commit()  # 触发 after_commit → 清缓存

    assert (user.id, None) not in cap_module._CAP_CACHE  # 缓存已清
    cap2 = compute_capability_set(db_session, user)
    assert cap2.allowed_tools == ["a", "b"]  # 反映新配置（被收回/新增的工具）


def test_cache_not_invalidated_on_rollback(db_session):
    """flush 后回滚不清缓存——改动未落库，缓存仍有效（flush→commit 标记被丢弃）。"""
    clear_capability_cache()
    db_session.add(
        Role(
            name="eng",
            config=RoleConfig(allowed_tools=["a"], knowledge_scope_tags=["eng"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "zane", role="eng")
    compute_capability_set(db_session, user)
    assert (user.id, None) in cap_module._CAP_CACHE

    role = db_session.exec(select(Role).where(Role.name == "eng")).first()
    role.config = RoleConfig(allowed_tools=["forbidden"], knowledge_scope_tags=["eng"], level=1)
    db_session.flush()  # after_flush 置标记
    db_session.rollback()  # after_rollback 丢弃标记

    assert (user.id, None) in cap_module._CAP_CACHE  # 缓存仍在


def test_unrelated_commit_keeps_cache(db_session):
    """无关表（如 AuditLog）提交不清缓存——失效只针对能力集输入表。"""
    clear_capability_cache()
    db_session.add(
        Role(
            name="eng",
            config=RoleConfig(allowed_tools=["a"], knowledge_scope_tags=["eng"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "uma", role="eng")
    db_session.commit()  # 先把 Role+User 提交干净（此事务含相关表，会清空缓存——但此刻缓存本就空）
    # 缓存填充：此时 session 无待提交的相关表变更
    compute_capability_set(db_session, user)
    assert (user.id, None) in cap_module._CAP_CACHE

    # 新事务只写 AuditLog（非能力集输入表）并提交
    from bycall.governance.models import AuditLog

    db_session.add(AuditLog(user_name="probe", action="chat", decision="allowed"))
    db_session.commit()  # 仅 AuditLog 脏 → after_flush 未置相关标记 → 不清缓存

    assert (user.id, None) in cap_module._CAP_CACHE  # 缓存仍在


def test_permission_error_not_cached(db_session):
    """禁用用户的 PermissionError 不缓存——重新启用后立即生效，不留陈旧拒绝。"""
    clear_capability_cache()
    db_session.add(
        Role(
            name="sales",
            config=RoleConfig(allowed_tools=["kb_search"], knowledge_scope_tags=["sales"], level=1),
        )
    )
    db_session.flush()
    user = _make_user(db_session, "vera", role="sales", is_active=False)
    with pytest.raises(PermissionError, match="用户已被禁用"):
        compute_capability_set(db_session, user)
    assert (user.id, None) not in cap_module._CAP_CACHE  # 拒绝结果未缓存

    # 重新启用 → 立即可算（未被陈旧拒绝阻塞）
    user.is_active = True
    db_session.flush()
    cap = compute_capability_set(db_session, user)
    assert cap.role == "sales"
    assert cap.allowed_tools == ["kb_search"]
