"""策略引擎单测：验证「用户+角色 → 能力集」翻译规则。

覆盖：
- admin 内置角色（通配 + 最高职级）
- 普通用户按 Role.config 收窄
- 角色未定义 / 用户被禁用 → PermissionError
- 激活系统提示词取值与兜底
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.governance.capability import (
    CapabilitySet,
    compute_capability_set,
    get_active_system_prompt,
)
from bycall.governance.models import Role, RoleConfig, SystemPrompt, User


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
