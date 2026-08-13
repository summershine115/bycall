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
    OnDemandSkill,
    build_skill_markdown,
    compute_capability_set,
    get_enabled_skills_by_names,
)
from bycall.governance.models import Role, RoleConfig, Skill, User
from bycall.tools.builtins import _load_skill_redactor


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


def _make_skill(
    session,
    name: str,
    content: str = "领域知识内容",
    enabled: bool = True,
    disclosure: str = "always",
) -> Skill:
    skill = Skill(
        name=name,
        description=f"{name} 描述",
        content=content,
        is_enabled=enabled,
        disclosure=disclosure,
    )
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
        """按 name 列表返回启用的 skill（**保留 admin 配置顺序**，非 name 字母序）。"""
        _make_skill(db_session, "alpha", "A内容")
        _make_skill(db_session, "beta", "B内容")

        # 传入顺序 [beta, alpha] → 输出也是 [beta, alpha]（与字母序相反，固化顺序语义）。
        result = get_enabled_skills_by_names(db_session, ["beta", "alpha"])
        assert [s.name for s in result] == ["beta", "alpha"]

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
        assert build_skill_markdown(always_skills=[]) == ""

    def test_single_skill_format(self, db_session):
        """单个 skill 格式：## 技能：{name}\\n{content}。"""
        _make_skill(db_session, "solo", "独奏内容")
        skills = get_enabled_skills_by_names(db_session, ["solo"])
        md = build_skill_markdown(always_skills=skills)
        assert "## 技能：solo" in md
        assert "独奏内容" in md

    def test_multiple_skills_joined_by_blank_line(self, db_session):
        """多个 skill 用空行分隔，各自独立小节。"""
        _make_skill(db_session, "a", "内容A")
        _make_skill(db_session, "b", "内容B")
        skills = get_enabled_skills_by_names(db_session, ["a", "b"])
        md = build_skill_markdown(always_skills=skills)
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
        """角色绑定多个 skill → 全部注入，**保留 admin 配置顺序**（非 name 字母序）。"""
        _make_skill(db_session, "zeta", "Z内容")
        _make_skill(db_session, "alpha", "A内容")
        # config 里 zeta 在前 → 注入顺序也 zeta 在前（admin 可控优先级）。
        _make_role_with_skills(db_session, "multi", ["zeta", "alpha"])
        _make_user(db_session, "multi-user", "multi")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("multi-user")).first()
        )

        # 保留 admin 配置顺序：zeta 在 alpha 之前（与 name 字母序相反，固化顺序语义）。
        assert cap.skills == ["zeta", "alpha"]
        assert "A内容" in cap.skill_markdown
        assert "Z内容" in cap.skill_markdown
        # 注入 markdown 同样保持配置顺序：zeta 段在 alpha 段之前。
        assert cap.skill_markdown.index("Z内容") < cap.skill_markdown.index("A内容")


class TestProgressiveDisclosure:
    """渐进式披露治理：on_demand skill 只进索引不进正文，全文经 _load_skill_redactor 按需返回。"""

    def test_on_demand_skill_in_on_demand_skills_not_body(self, db_session):
        """on_demand skill → on_demand_skills 有摘要，skill_markdown 无全文。"""
        _make_skill(db_session, "compliance", "按需内容XYZ", disclosure="on_demand")
        _make_role_with_skills(db_session, "auditor", ["compliance"])
        _make_user(db_session, "auditor1", "auditor")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("auditor1")).first()
        )

        assert cap.skills == ["compliance"]
        assert len(cap.on_demand_skills) == 1
        od = cap.on_demand_skills[0]
        assert od.name == "compliance"
        assert od.description == "compliance 描述"
        assert od.content == "按需内容XYZ"
        assert "按需内容XYZ" not in cap.skill_markdown
        assert "## 按需技能索引" in cap.skill_markdown
        assert "compliance 描述" in cap.skill_markdown

    def test_always_skill_still_in_skill_markdown(self, db_session):
        """always 披露 skill 仍然全文进 skill_markdown，无 on_demand 则无索引。"""
        _make_skill(db_session, "core", "始终内容ABC")
        _make_role_with_skills(db_session, "core-role", ["core"])
        _make_user(db_session, "core-user", "core-role")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("core-user")).first()
        )

        assert "始终内容ABC" in cap.skill_markdown
        assert "## 按需技能索引" not in cap.skill_markdown
        assert cap.on_demand_skills == []

    def test_mixed_disclosure_skills(self, db_session):
        """混合 disclosure → always 全文进正文，on_demand 仅进索引。"""
        _make_skill(db_session, "resident", "常驻")
        _make_skill(db_session, "lazy", "按需内容", disclosure="on_demand")
        _make_role_with_skills(db_session, "mixed-role", ["resident", "lazy"])
        _make_user(db_session, "mixed-user", "mixed-role")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("mixed-user")).first()
        )

        assert cap.skills == ["resident", "lazy"]
        assert "常驻" in cap.skill_markdown
        assert "按需内容" not in cap.skill_markdown
        assert "## 按需技能索引" in cap.skill_markdown
        assert "lazy" in cap.skill_markdown
        assert "lazy 描述" in cap.skill_markdown
        assert len(cap.on_demand_skills) == 1
        assert cap.on_demand_skills[0].content == "按需内容"

    def test_load_skill_redactor_gates(self, db_session):
        """_load_skill_redactor 按名返回全文，未绑定 skill 返回错误。"""
        _make_skill(db_session, "secret", "机密内容", disclosure="on_demand")
        _make_role_with_skills(db_session, "spy", ["secret"])
        _make_user(db_session, "spy-user", "spy")

        cap = compute_capability_set(
            db_session, db_session.exec(User.select_by_name("spy-user")).first()
        )

        assert _load_skill_redactor("secret", cap) == "机密内容"
        result = _load_skill_redactor("unknown", cap)
        assert "未找到" in result
        assert "无权" in result

    def test_build_skill_markdown_on_demand_only(self):
        """仅 on_demand skill → 只有索引部分，不含全文。"""
        guide = OnDemandSkill(name="guide", description="指南描述", content="指南全文")
        result = build_skill_markdown(always_skills=[], on_demand_skills=[guide])

        assert "## 按需技能索引" in result
        assert "- **guide**：指南描述" in result
        assert "指南全文" not in result

    def test_build_skill_markdown_both_empty(self):
        """空列表 → 返回空串（向后兼容）。"""
        assert build_skill_markdown([], []) == ""
