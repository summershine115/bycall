"""Skill 导入导出单测：SKILL.md 序列化 & ZIP 打包/解包。"""

from __future__ import annotations

import io

import pytest
from sqlmodel import create_engine, select

from bycall.admin.skill_io import (
    export_skills_to_zip,
    import_skills_from_zip,
    parse_skill_md,
    skill_to_skill_md,
)
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.models import Skill


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db（含默认 member/admin/greeting-guide skill）。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "vectors"))
    get_settings.cache_clear()
    set_engine(
        create_engine(
            f"sqlite:///{tmp_path}/test.db",
            connect_args={"check_same_thread": False},
        )
    )
    init_db()
    with session_scope() as s:
        yield s
    get_settings.cache_clear()


# ============ SKILL.md 往返 ============


class TestSkillMarkdownRoundtrip:
    def test_roundtrip_skill_to_md_to_dict(self):
        skill = Skill(
            name="test-skill",
            description="测试描述",
            content="# 标题\n正文内容",
            disclosure="on_demand",
            is_enabled=True,
        )

        md_string = skill_to_skill_md(skill)

        assert md_string.startswith("---\n")
        assert "name: test-skill" in md_string
        assert "disclosure: on_demand" in md_string
        assert "# 标题\n正文内容" in md_string

        result = parse_skill_md(md_string)
        assert result["name"] == "test-skill"
        assert result["description"] == "测试描述"
        assert result["content"] == "# 标题\n正文内容"
        assert result["disclosure"] == "on_demand"
        assert result["is_enabled"] is True

    def test_parse_minimal_frontmatter(self):
        result = parse_skill_md("---\nname: minimal\n---\nbody")
        assert result["name"] == "minimal"
        assert result["description"] == ""
        assert result["content"] == "body"
        assert result["disclosure"] == "always"
        assert result["is_enabled"] is True

    def test_parse_missing_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            parse_skill_md("---\ndescription: no name\n---\nbody")

    def test_parse_missing_frontmatter_raises(self):
        with pytest.raises(ValueError):
            parse_skill_md("no frontmatter at all")

    def test_parse_invalid_yaml_raises(self):
        with pytest.raises(ValueError):
            parse_skill_md("---\n{\n---\nbad")


# ============ ZIP 往返 ============


class TestSkillZipRoundtrip:
    def test_export_import_roundtrip(self, db_session):
        with session_scope() as s:
            s.add(Skill(name="alpha-skill", description="Alpha 描述", content="Alpha 内容", disclosure="always"))
            s.add(
                Skill(
                    name="beta-skill",
                    description="Beta 描述",
                    content="Beta 内容",
                    disclosure="on_demand",
                    is_enabled=False,
                )
            )
            s.commit()

        with session_scope() as s:
            skills = s.exec(
                select(Skill).where(Skill.name.in_(["alpha-skill", "beta-skill"])).order_by(Skill.name)
            ).all()

            buf = io.BytesIO()
            export_skills_to_zip(skills, buf)
            buf.seek(0)

        result = import_skills_from_zip(buf.read())
        assert len(result["skills"]) == 2
        assert len(result["errors"]) == 0

        alpha = next(s for s in result["skills"] if s["name"] == "alpha-skill")
        assert alpha["content"] == "Alpha 内容"
        assert alpha["disclosure"] == "always"
        assert alpha["is_enabled"] is True

        beta = next(s for s in result["skills"] if s["name"] == "beta-skill")
        assert beta["content"] == "Beta 内容"
        assert beta["disclosure"] == "on_demand"
        assert beta["is_enabled"] is False

    def test_export_empty_skills(self, db_session):
        buf = io.BytesIO()
        export_skills_to_zip([], buf)
        buf.seek(0)

        import zipfile
        zf = zipfile.ZipFile(buf, "r")
        assert len(zf.namelist()) == 0
        zf.close()

    def test_import_invalid_zip_graceful(self):
        result = import_skills_from_zip(b"not a zip")
        assert result["skills"] == []
        assert len(result["errors"]) >= 1
        assert "ZIP" in result["errors"][0]["reason"]
