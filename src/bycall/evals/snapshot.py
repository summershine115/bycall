"""治理配置版本快照 + 变更 diff（execution-plan T2.3 离线回放）。

快照只存**可比较的指纹**（内容 SHA-256、配置 JSON），不存密钥/密文，也不存提示词
正文——快照文件可以安全地提交进仓库 / 评审记录。回放 diff 时按路径把每条变更归类到
``kind``（prompt/skill/model/retrieval/tool/role），runner 据此选择对应评测子集。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session, select

# 与 core.CHANGE_KINDS 对齐；路径 → 变更类型的分类规则集中在这里。
_KINDS = ("prompt", "skill", "model", "retrieval", "tool", "role")


def _sha256(text: str) -> str:
    """内容指纹（不落明文，快照可提交）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mask_base_url(url: str) -> str:
    """脱敏 base_url：只保留 scheme + host + port，避免 query 里的 token 进快照。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme else parts.netloc
    except ValueError:
        return url[:80]


@dataclass
class ConfigSnapshot:
    """一次治理配置快照（提示词 / Skill / 角色 / 模型 / 知识库 / 工具 / MCP）。"""

    captured_at: str = ""
    source: str = ""  # db / file
    prompt: dict[str, Any] = field(default_factory=dict)
    skills: list[dict[str, Any]] = field(default_factory=list)
    roles: dict[str, Any] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    knowledge_bases: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfigSnapshot:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> ConfigSnapshot:
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(raw))


def capture_config(session: Session) -> ConfigSnapshot:
    """从当前 DB + 进程内工具注册表抓一份治理配置快照。

    不依赖全局 ``bycall.db`` 引擎——传入任意 Session 即可（评测 / 发布门 / 回放通用）。
    """
    from bycall.governance.models import (
        KnowledgeBase,
        MCPServer,
        Model,
        Role,
        Skill,
        SystemPrompt,
    )
    from bycall.tools.registry import get_all_tools

    prompt = {}
    active = session.exec(SystemPrompt.select_active()).first()
    if active is not None:
        prompt = {
            "version": active.version,
            "label": active.label,
            "content_sha256": _sha256(active.content),
            "is_active": bool(active.is_active),
        }

    skills = []
    for s in session.exec(Skill.select_all()).all():
        skills.append(
            {
                "name": s.name,
                "description_sha256": _sha256(s.description),
                "content_sha256": _sha256(s.content),
                "is_enabled": bool(s.is_enabled),
                "disclosure": s.disclosure,
            }
        )

    roles = {}
    for r in session.exec(select(Role).order_by(Role.name)).all():
        roles[r.name] = {
            "description": r.description,
            "config": r.config.model_dump(mode="json"),
        }

    models = []
    for m in session.exec(select(Model).order_by(Model.id)).all():
        models.append(
            {
                "id": m.id,
                "name": m.name,
                "model_type": m.model_type,
                "provider": m.provider,
                "base_url": _mask_base_url(m.base_url),
                "embed_dimension": m.embed_dimension,
                "is_active": bool(m.is_active),
            }
        )

    kbs = []
    for kb in session.exec(KnowledgeBase.select_all()).all():
        kbs.append(
            {
                "id": kb.id,
                "name": kb.name,
                "embedding_model_id": kb.embedding_model_id,
                "dimension": kb.dimension,
                "visible_to_roles": list(kb.visible_to_roles or []),
                "chunk_size": kb.chunk_size,
                "chunk_overlap": kb.chunk_overlap,
            }
        )

    tools = []
    for spec in get_all_tools():
        tools.append(
            {
                "name": spec.name,
                "description_sha256": _sha256(spec.description),
                "parameters_sha256": _sha256(json.dumps(spec.parameters, sort_keys=True)),
                "source": spec.source,
                "requires_approval": bool(spec.requires_approval),
            }
        )

    mcp_servers = []
    for server in session.exec(MCPServer.select_all()).all():
        mcp_servers.append(
            {
                "name": server.name,
                "transport": server.transport,
                "is_enabled": bool(server.is_enabled),
            }
        )

    return ConfigSnapshot(
        captured_at=datetime.now(UTC).isoformat(),
        source="db",
        prompt=prompt,
        skills=skills,
        roles=roles,
        models=models,
        knowledge_bases=kbs,
        tools=tools,
        mcp_servers=mcp_servers,
    )


def _flatten(value: Any, prefix: str, kind: str) -> dict[str, tuple[str, Any]]:
    """把 JSON 安全值拍平成 ``path -> (kind, value)``（dict 递归、list 按序号）。"""
    out: dict[str, tuple[str, Any]] = {}
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            out.update(_flatten(child, f"{prefix}.{key}", kind))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            out.update(_flatten(child, f"{prefix}[{index}]", kind))
    else:
        out[prefix] = (kind, value)
    return out


def _flat_snapshot(snap: ConfigSnapshot) -> dict[str, tuple[str, Any]]:
    """快照 → path 扁平表。角色配置按字段语义细分 kind（安全子集选择的依据）。"""
    out: dict[str, tuple[str, Any]] = {}
    out.update(_flatten(snap.prompt, "prompt", "prompt"))
    for skill in snap.skills:
        name = skill.get("name", "?")
        out.update(_flatten(skill, f"skills.{name}", "skill"))
    for role_name, role in snap.roles.items():
        config = role.get("config", {})
        out.update(_flatten(role.get("description", ""), f"roles.{role_name}.description", "role"))
        for key, value in config.items():
            kind = _kind_for_role_field(key)
            out.update(_flatten(value, f"roles.{role_name}.config.{key}", kind))
    for model in snap.models:
        model_id = model.get("id") if model.get("id") is not None else model.get("name")
        out.update(_flatten(model, f"models.{model_id}", "model"))
    for kb in snap.knowledge_bases:
        kb_id = kb.get("id") if kb.get("id") is not None else kb.get("name")
        out.update(_flatten(kb, f"kbs.{kb_id}", "retrieval"))
    for tool in snap.tools:
        out.update(_flatten(tool, f"tools.{tool.get('name', '?')}", "tool"))
    for server in snap.mcp_servers:
        out.update(_flatten(server, f"mcp.{server.get('name', '?')}", "tool"))
    return out


def _kind_for_role_field(field_name: str) -> str:
    """角色配置字段 → 变更类型。分类口径：谁能用什么工具 / 看什么知识 / 用什么提示词。"""
    if field_name == "allowed_tools":
        return "tool"
    if field_name == "knowledge_scope_tags":
        return "retrieval"
    if field_name == "system_prompt_version":
        return "prompt"
    if field_name == "skills":
        return "skill"
    return "role"  # level / context_budget 等其余字段归角色维度


_MISSING = object()


@dataclass(frozen=True)
class ConfigChange:
    """一条新旧配置差异（回放 / 发布门的最小单位）。"""

    kind: str
    path: str
    old: Any = None
    new: Any = None
    added: bool = False
    removed: bool = False

    def describe(self, limit: int = 120) -> str:
        old = _clip(self.old, limit)
        new = _clip(self.new, limit)
        if self.added:
            return f"+ {self.path} = {new}"
        if self.removed:
            return f"- {self.path} = {old}"
        return f"~ {self.path}: {old} → {new}"


def _clip(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def diff_configs(baseline: ConfigSnapshot, candidate: ConfigSnapshot) -> list[ConfigChange]:
    """比较两份快照，返回全部差异（含增删）。"""
    old_flat = _flat_snapshot(baseline)
    new_flat = _flat_snapshot(candidate)
    changes: list[ConfigChange] = []
    for path in sorted(set(old_flat) | set(new_flat)):
        old_entry = old_flat.get(path)
        new_entry = new_flat.get(path)
        old = old_entry[1] if old_entry is not None else _MISSING
        new = new_entry[1] if new_entry is not None else _MISSING
        if old is _MISSING:
            assert new_entry is not None
            changes.append(ConfigChange(kind=new_entry[0], path=path, new=new, added=True))
            continue
        if new is _MISSING:
            assert old_entry is not None
            changes.append(ConfigChange(kind=old_entry[0], path=path, old=old, removed=True))
            continue
        if old != new:
            # kind 优先取新侧（路径未变时两侧一致；取新侧保证分类跟随候选）。
            assert new_entry is not None
            changes.append(ConfigChange(kind=new_entry[0], path=path, old=old, new=new))
    return changes


def changed_kinds(changes: list[ConfigChange]) -> set[str]:
    """返回本次变更涉及的 kind 集合。"""
    return {c.kind for c in changes}


def describe_changes(changes: list[ConfigChange]) -> str:
    """diff 的人类可读文本（CLI / 发布门报告）。"""
    if not changes:
        return "无配置差异"
    return "\n".join(c.describe() for c in changes)


def diff_verdict(
    baseline_report,
    candidate_report,
    changes: list[ConfigChange],
) -> dict[str, Any]:
    """回放结论：比较新旧两份评测报告，给出「是否越权/回退」的机器可读判定。

    判定规则：
    - 候选任一条用例失败 → ``candidate_failed=True``（发布门红灯）。
    - 旧跑通过、新跑失败 → ``regressions``（行为回退）。
    - 旧跑失败、新跑通过 → ``improvements``（修复）。
    """
    base_map = {r.case_id: r for r in baseline_report.results}
    cand_map = {r.case_id: r for r in candidate_report.results}
    regressions = []
    improvements = []
    for case_id in sorted(set(base_map) | set(cand_map)):
        old_passed = base_map[case_id].passed if case_id in base_map else None
        new_passed = cand_map[case_id].passed if case_id in cand_map else None
        if old_passed is True and new_passed is False:
            regressions.append(case_id)
        elif old_passed is False and new_passed is True:
            improvements.append(case_id)
    candidate_failed = any(not r.passed for r in candidate_report.results)
    return {
        "changed_kinds": sorted(changed_kinds(changes)),
        "changes": describe_changes(changes),
        "baseline": {
            "total": baseline_report.total,
            "passed": baseline_report.passed,
            "failed": baseline_report.failed,
        },
        "candidate": {
            "total": candidate_report.total,
            "passed": candidate_report.passed,
            "failed": candidate_report.failed,
        },
        "regressions": regressions,
        "improvements": improvements,
        "candidate_failed": candidate_failed,
        # 出口门答案：没有新增越权/回退，且候选本身全绿。
        "verdict": "通过：未发现越权或质量回退" if not candidate_failed else "失败：候选评测不通过",
    }


def select_cases_for_changes(cases: list, changes: list[ConfigChange]) -> list:
    """按变更类型选择评测子集（T2.3 验收：按变更类型选对应子集）。"""
    kinds = changed_kinds(changes)
    if not kinds:
        return []
    return [c for c in cases if not c.change_types or set(c.change_types) & kinds]


__all__ = [
    "ConfigChange",
    "ConfigSnapshot",
    "capture_config",
    "changed_kinds",
    "describe_changes",
    "diff_configs",
    "diff_verdict",
    "select_cases_for_changes",
]
