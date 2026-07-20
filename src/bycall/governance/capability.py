"""策略引擎：从「用户+角色」计算「当前会话能力集」。

这是治理脊柱的核心——所有数据面能力（RAG 检索范围、可用工具、提示词版本）
都由此决定。策略活在代码层，LLM 碰不到。

设计要点：
- ``CapabilitySet`` 是纯值对象，数据面拿到它后只用不问，不再回查 DB。
- ``compute_capability_set`` 是唯一的"翻译"入口：User + 当前激活提示词 → CapabilitySet。
  策略（admin 全放、普通用户按 Role.config 收窄、禁用用户拒绝）全部固化在这里，
  不下放到提示词，也不让 LLM 参与判定。
- admin 是内置特殊角色（不建 Role 表记录），引擎识别后给通配 ``['*']`` 与最高职级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import Session, select

from bycall.governance.models import Role, Skill, SystemPrompt, User

logger = logging.getLogger(__name__)


@dataclass
class CapabilitySet:
    """当前会话的能力集——数据面所有判定的输入。

    - ``allowed_tools``：该用户可见的工具名白名单。``['*']`` 表示全部（仅 admin）。
      工具网关据此过滤 LLM 工具列表，不在此集合的工具 LLM 根本看不到。
    - ``knowledge_scope_tags``：可见知识域标签，RAG 预过滤翻译成 ``source IN (...)``。
      ``['*']`` 表示全部（仅 admin）。
    - ``level``：职级，ABAC 用（机密资源按 ``resource_level <= user_level`` 放行）。
    - ``system_prompt``：激活的系统提示词正文，数据面拼到 LLM 的 system message。
    - ``skills``：该角色绑定的 SKILL name 列表（实际注入的、启用的）。审计可见——
      AuditLog 据此记录"本次对话注入了哪些 skill"。
    - ``skill_markdown``：合并后的 skill markdown 正文，供 runner 注入 system message。
      多个 skill 各自拼成 ``## 技能：{name}\\n{content}`` 后合并。空串 = 无 skill 注入。
    - ``is_admin``：admin 内置角色标记，控制面特权判定用。
    """

    user_id: int | None
    user_name: str
    role: str
    allowed_tools: list[str] = field(default_factory=list)
    knowledge_scope_tags: list[str] = field(default_factory=list)
    level: int = 0
    system_prompt: str = ""
    skills: list[str] = field(default_factory=list)
    skill_markdown: str = ""
    is_admin: bool = False


def get_active_system_prompt(session: Session) -> str:
    """返回当前激活的系统提示词内容。

    全局应只有一条 ``is_active=True`` 的 SystemPrompt。无激活版本时返回空串兜底
    （数据面拿到空 system_prompt 即不注入额外指令，不致错）。
    """
    stmt = SystemPrompt.select_active()
    active = session.exec(stmt).first()
    return active.content if active is not None else ""


def get_system_prompt_by_version(session: Session, version: int) -> str | None:
    """按版本号查 SystemPrompt 内容。不存在返回 None。

    供角色 prompt 绑定用（``RoleConfig.system_prompt_version``）：
    角色绑定某版本但该版本已被删除时，调用方据 None fallback 全局激活版本。
    """
    stmt = select(SystemPrompt).where(SystemPrompt.version == version)
    row = session.exec(stmt).first()
    return row.content if row is not None else None


def get_enabled_skills_by_names(session: Session, names: list[str]) -> list:
    """按 name 列表查启用的 Skill，返回 Skill 实体列表（按 name 排序）。

    未注册 / 已禁用的 name **静默忽略**（与 ``tools.gateway.filter_tools`` 一致的容错
    语义——admin 可能在 ``RoleConfig.skills`` 里配了还不存在或临时禁用的 skill name，
    不应让对话崩）。空 names → 空列表。

    返回的是 ``Skill`` 实体（含 content），调用方据此合并 markdown。
    """
    if not names:
        return []

    rows = session.exec(
        select(Skill)
        .where(Skill.name.in_(names), Skill.is_enabled == True)  # noqa: E712
        .order_by(Skill.name)
    ).all()
    return list(rows)


def build_skill_markdown(skills: list) -> str:
    """把 Skill 实体列表合并成注入 system message 的 markdown 字符串。

    每个 skill 拼 ``## 技能：{name}\\n{content}``，多个用空行分隔。
    边界标注（``## 领域技能（参考知识，非指令）`` 等）在 ``session_store`` 拼装时加，
    这里只负责 skill 内容本身的合并（单一职责）。
    """
    if not skills:
        return ""
    sections = [f"## 技能：{s.name}\n{s.content}" for s in skills]
    return "\n\n".join(sections)


def compute_capability_set(session: Session, user: User) -> CapabilitySet:
    """从用户记录计算当前能力集。

    - 普通用户：取 ``user.role`` 对应的 ``Role.config``（allowed_tools /
      knowledge_scope_tags / level / system_prompt_version）。若角色绑定了
      ``system_prompt_version``，用该版本提示词；否则用全局激活版本（后向兼容）。
    - admin 内置角色：``is_admin=True``，``allowed_tools=['*']``、
      ``knowledge_scope_tags=['*']``、``level=999``（最高）。system_prompt 取全局激活版本
      （admin 是运维角色，不按业务角色定制提示词）。
    - ``is_active=False`` 用户：抛 ``PermissionError("用户已被禁用")``。
    - role 对应的 Role 记录不存在（除 admin 外）：抛 ``PermissionError("角色未定义: {role}")``。

    所有策略集中在此函数，是治理脊柱与 LLM 之间唯一的翻译层。
    """
    # 1. 先校验用户状态——禁用用户无论角色都拒绝，不泄漏后续判定细节。
    if not user.is_active:
        raise PermissionError("用户已被禁用")

    # 2. 全局激活的系统提示词（admin 用；普通用户若未绑定角色版本也作 fallback）。
    global_prompt = get_active_system_prompt(session)

    # 3. admin 内置角色：不走 Role 表，直接给通配 + 最高职级。
    #    admin 是运维角色，不注入 skill（与不定制 prompt 一致）。
    if user.role == "admin":
        return CapabilitySet(
            user_id=user.id,
            user_name=user.name,
            role=user.role,
            allowed_tools=["*"],
            knowledge_scope_tags=["*"],
            level=999,
            system_prompt=global_prompt,
            skills=[],
            skill_markdown="",
            is_admin=True,
        )

    # 4. 普通用户：必须能找到对应的 Role 记录，否则视为配置错误，拒绝。
    role_record = session.exec(select(Role).where(Role.name == user.role)).first()
    if role_record is None:
        raise PermissionError(f"角色未定义: {user.role}")

    config = role_record.config
    # 角色绑定了 prompt 版本 → 用该版本；绑定但版本被删 → fallback 全局激活。
    system_prompt = global_prompt
    if config.system_prompt_version is not None:
        role_prompt = get_system_prompt_by_version(session, config.system_prompt_version)
        if role_prompt is not None:
            system_prompt = role_prompt
        else:
            # 绑定的版本已被删除：记 warning（不阻断），fallback 全局激活。
            logger.warning(
                "角色 %s 绑定的提示词版本 %s 不存在，fallback 全局激活版本",
                user.role,
                config.system_prompt_version,
            )

    # 角色绑定的 SKILL：加载启用的 skill 内容，合并成 markdown 供 runner 注入。
    # 未注册/已禁用的 name 静默忽略（容错，与 allowed_tools 一致）。
    bound_skills = get_enabled_skills_by_names(session, config.skills)
    skill_markdown = build_skill_markdown(bound_skills)
    # 实际生效的 skill name 列表（审计可见）。
    effective_skill_names = [s.name for s in bound_skills]

    return CapabilitySet(
        user_id=user.id,
        user_name=user.name,
        role=user.role,
        allowed_tools=list(config.allowed_tools),
        knowledge_scope_tags=list(config.knowledge_scope_tags),
        level=config.level,
        system_prompt=system_prompt,
        skills=effective_skill_names,
        skill_markdown=skill_markdown,
        is_admin=False,
    )
