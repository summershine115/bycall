"""Skill 导入导出（ZIP 压缩包，Agent Skills 规范格式）。

纯 I/O + schema 逻辑，无 FastAPI 依赖。提供 SKILL.md 前端元数据序列化
与 ZIP 打包/解包，供 admin API 路由调用。
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import yaml


def skill_to_skill_md(skill) -> str:
    """把 Skill ORM 实例转成 SKILL.md 字符串（YAML frontmatter + Markdown body）。"""
    frontmatter: dict = {
        "name": skill.name,
        "description": skill.description or "",
        "metadata": {
            "source": "bycall",
            "disclosure": skill.disclosure or "always",
            "is_enabled": skill.is_enabled,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }
    yaml_part = yaml.dump(
        frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False
    ).rstrip("\n")
    body = skill.content or ""
    return f"---\n{yaml_part}\n---\n{body}"


def parse_skill_md(content: str) -> dict:
    """解析 SKILL.md 字符串。

    返回 ``{name, description, content, disclosure, is_enabled}``。
    前端 YAML 中 ``metadata`` 为可选；无 metadata 时 disclosure/is_enabled 取默认值。
    """
    if not content.startswith("---\n"):
        raise ValueError("缺少 YAML frontmatter（必须以 '---\\n' 开头）")

    # 找第二个 ``---`` 分隔符
    end_idx = content.find("\n---\n", 4)
    if end_idx == -1:
        raise ValueError("YAML frontmatter 未正确关闭（缺少第二个 '---'）")

    yaml_str = content[4:end_idx]
    try:
        frontmatter = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 解析失败: {e}") from e

    if not isinstance(frontmatter, dict):
        raise ValueError("YAML frontmatter 必须是映射")

    name = frontmatter.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("缺少必需字段 'name'")

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        description = ""

    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    disclosure = metadata.get("disclosure", "always")
    if disclosure not in ("always", "on_demand"):
        disclosure = "always"

    is_enabled = metadata.get("is_enabled", True)
    if not isinstance(is_enabled, bool):
        is_enabled = True

    # body 是第二个 ``---`` 之后的内容（跳过 ``\n---\n`` 四个字符）
    body = content[end_idx + 5 :]

    return {
        "name": name.strip(),
        "description": description.strip(),
        "content": body,
        "disclosure": disclosure,
        "is_enabled": is_enabled,
    }


def export_skills_to_zip(skills: list, buffer) -> None:
    """把 Skill 列表写成 ZIP 压缩包到可写 buffer（BytesIO 或文件对象）。

    ZIP 结构：每个 skill 一个目录 ``{name}/SKILL.md``。
    """
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for skill in skills:
            name = skill.name.strip()
            # 同名 skill 只写一次（数据异常时的防御）
            if name in seen:
                continue
            seen.add(name)
            md_content = skill_to_skill_md(skill)
            zf.writestr(f"{name}/SKILL.md", md_content)


def import_skills_from_zip(zip_buffer, on_conflict: str = "skip") -> dict:
    """解析 ZIP buffer，提取所有 ``*/SKILL.md`` 条目。

    返回 ``{"skills": [...], "errors": [...]}``——不写 DB，由调用方 upsert。
    ``on_conflict`` 在此仅透传（冲突处理在 API handler 里做）。
    """
    skills: list[dict] = []
    errors: list[dict] = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_buffer), "r")
    except zipfile.BadZipFile as e:
        return {"skills": [], "errors": [{"entry": "(zip)", "reason": f"无效的 ZIP 文件: {e}"}]}

    with zf:
        for entry in zf.namelist():
            # 只处理 {name}/SKILL.md，跳过目录/隐文件/其他格式
            if entry.endswith("/") or entry.startswith("."):
                continue
            if not entry.endswith("/SKILL.md"):
                continue

            # entry 格式: "{name}/SKILL.md"，提取 skill name
            skill_name = entry[: -len("/SKILL.md")]
            if not skill_name:
                errors.append({"entry": entry, "reason": "无法提取 skill name"})
                continue

            try:
                raw = zf.read(entry).decode("utf-8")
            except UnicodeDecodeError as e:
                errors.append({"entry": entry, "reason": f"编码错误: {e}"})
                continue

            try:
                parsed = parse_skill_md(raw)
            except ValueError as e:
                errors.append({"entry": entry, "reason": str(e)})
                continue

            parsed["_entry"] = entry
            skills.append(parsed)

    return {"skills": skills, "errors": errors}
