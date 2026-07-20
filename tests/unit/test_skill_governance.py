"""SKILL 治理单测：验证角色绑定的 skill → CapabilitySet 翻译规则。

覆盖 capability 引擎的 skill 分发：
- 角色绑定 skills → CapabilitySet.skills / skill_markdown 含对应内容
- 绑定的 skill 禁用 → 不注入（静默忽略）
- 绑定的 skill name 不存在 → 不报错（静默忽略）
- admin 内置角色 → skills=[] / skill_markdown=""（运维角色不注入）
- 空绑定 → skill_markdown=""
- 辅助函数 get_enabled_skills_by_names / build_skill_markdown 行为

范式与 test_capability.py 一致：临时 SQLite + init_db + session_scope。
"""

from __future__ import annotations

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.governance.capability import (
    build_skill_markdown,
    compute_capability_set,
    get_enabled_skills_by_names,
)
from bycall.governance.models import Role, RoleConfig, Skill, User


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含默认 member/admin/greeting-guide skill）。"""
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


def _make_role_with_skills(session, name: str, skills: list[str]) -> Role:
    """建一个带 skills 绑定的角色。"""
    role = Role(
        name=name,
        description="测试角色",
        config=RoleConfig(
            allowed_tools=[],
            knowledge_scope_tags=["public"],
            level=1,
            skills=skills,
        ),
    )
    session.add(role)
    session.flush()
    return role


def _make_skill(session, name: str, content: str = "领域知识内容", enabled: bool = True) -> Skill:
    skill = Skill(name=name, description=f"{name} 描述", content=content, is_enabled=enabled)
    session.add(skill)
    session.flush()
    return skill


def _make_user(session, name: str, role: str) -> User:
    user = User(name=name, display_name=name, role=role, is_active=True)
    session.add(user)
    session.flush()
    return user


# ============ 辅助函数 ============


class TestGetEnabledSkillsByNames:
    def test_returns_enabled_skills_matching_names(self, db_session):
        """按 name 列表返回启用的 skill（按 name 排序）。"""
        _make_skill(db_session, "alpha", "A内容")
        _make_skill(db_session, "beta", "B内容")

        result = get_enabled_skills_by_names(db_session, ["beta", "alpha"])
        assert [s.name for s in result] == ["alpha", "beta"]

    def test_disabled_skill_silently_ignored(self, db_session):
        """已禁用的 skill 静默忽略（与 filter_tools 容错语义一致）。"""
        _make_skill(db_session, "enabled-one", "启用的", enabled=True)
        _make_skill(db_session, "disabled-one", "禁用的", enabled=False)

        result = get_enabled_skills_by_names(db_session, ["enabled-one", "disabled-one"])
        assert [s.name for s in result] == ["enabled-one"]

    def test_nonexistent_name_silently_ignored(self, db_session):
        """未注册的 name 静默忽略（admin 可能配了还不存在的 skill）。"""
        _make_skill(db_session, "real", "真实的")
        result = get_enabled_skills_by_names(db_session, ["real", "nonexistent"])
        assert [s.name for s in result] == ["real"]

    def test_empty_names_returns_empty(self, db_session):
        """空 name 列表 → 空结果。"""
        _make_skill(db_session, "x", "x")
        assert get_enabled_skills_by_names(db_session, []) == []


class TestBuildSkillMarkdown:
    def test_empty_skills_returns_empty(self):
        assert build_skill_markdown([]) == ""

    def test_single_skill_format(self, db_session):
        """单个 skill 格式：## 技能：{name}\\n{content}。"""
        _make_skill(db_session, "solo", "独奏内容")
        skills = get_enabled_skills_by_names(db_session, ["solo"])
        md = build_skill_markdown(skills)
        assert "## 技能：solo" in md
        assert "独奏内容" in md

    def test_multiple_skills_joined_by_blank_line(self, db_session):
        """多个 skill 用空行分隔，各自独立小节。"""
        _make_skill(db_session, "a", "内容A")
        _make_skill(db_session, "b", "内容B")
        skills = get_enabled_skills_by_names(db_session, ["a", "b"])
        md = build_skill_markdown(skills)
        assert md.count("## 技能：") == 2
        assert "## 技能：a\n内容A" in md
        assert "## 技能：b\n内容B" in md


# ============ compute_capability_set 分发 ============


class TestSkillCapabilityDispatch:
    def test_bound_skills_injected_into_capability(self, db_session):
        """角色绑定 skills → CapabilitySet.skills 含 name + skill_markdown 含内容。"""
        _make_skill(db_session, "sales-playbook", "销售话术ABC")
        _make_role_with_skills(db_session, "sales", ["sales-playbook"])
        _make_user(db_session, "alice", "sales")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("alice")).first()
        )

        assert cap.skills == ["sales-playbook"]
        assert "销售话术ABC" in cap.skill_markdown
        assert "## 技能：sales-playbook" in cap.skill_markdown

    def test_disabled_bound_skill_not_injected(self, db_session):
        """绑定的 skill 被禁用 → 不注入（静默忽略，不报错）。"""
        _make_skill(db_session, "disabled-skill", "禁用内容", enabled=False)
        _make_role_with_skills(db_session, "r1", ["disabled-skill"])
        _make_user(db_session, "u1", "r1")

        cap = compute_capability_set(db_session, db_session.exec(User.select_by_name("u1")).first())

        assert cap.skills == []
        assert cap.skill_markdown == ""

    def test_nonexistent_bound_skill_ignored(self, db_session):
        """绑定的 skill name 不存在 → 静默忽略，不报错。"""
        _make_role_with_skills(db_session, "r2", ["ghost-skill"])
        _make_user(db_session, "u2", "r2")

        cap = compute_capability_set(db_session, db_session.exec(User.select_by_name("u2")).first())

        assert cap.skills == []
        assert cap.skill_markdown == ""

    def test_admin_role_no_skill_injection(self, db_session):
        """admin 内置角色 → skills=[] / skill_markdown=""（运维角色不注入）。"""
        _make_skill(db_session, "any-skill", "任何内容")
        admin = db_session.exec(User.select_by_name("admin")).first()
        assert admin is not None

        cap = compute_capability_set(db_session, admin)

        assert cap.is_admin is True
        assert cap.skills == []
        assert cap.skill_markdown == ""

    def test_empty_skills_binding(self, db_session):
        """角色未绑定 skill → skill_markdown 为空。"""
        _make_role_with_skills(db_session, "r3", [])
        _make_user(db_session, "u3", "r3")

        cap = compute_capability_set(db_session, db_session.exec(User.select_by_name("u3")).first())

        assert cap.skills == []
        assert cap.skill_markdown == ""

    def test_multiple_bound_skills_all_injected(self, db_session):
        """角色绑定多个 skill → 全部注入，按 name 排序。"""
        _make_skill(db_session, "zeta", "Z内容")
        _make_skill(db_session, "alpha", "A内容")
        _make_role_with_skills(db_session, "multi", ["zeta", "alpha"])
        _make_user(db_session, "multi-user", "multi")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("multi-user")).first()
        )

        # 按 name 排序
        assert cap.skills == ["alpha", "zeta"]
        assert "A内容" in cap.skill_markdown
        assert "Z内容" in cap.skill_markdown
