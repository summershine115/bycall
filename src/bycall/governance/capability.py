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

from dataclasses import dataclass, field

from sqlmodel import Session, select

from bycall.governance.models import Role, SystemPrompt, User


@dataclass
class CapabilitySet:
    """当前会话的能力集——数据面所有判定的输入。

    - ``allowed_tools``：该用户可见的工具名白名单。``['*']`` 表示全部（仅 admin）。
      工具网关据此过滤 LLM 工具列表，不在此集合的工具 LLM 根本看不到。
    - ``knowledge_scope_tags``：可见知识域标签，RAG 预过滤翻译成 ``source IN (...)``。
      ``['*']`` 表示全部（仅 admin）。
    - ``level``：职级，ABAC 用（机密资源按 ``resource_level <= user_level`` 放行）。
    - ``system_prompt``：激活的系统提示词正文，数据面拼到 LLM 的 system message。
    - ``is_admin``：admin 内置角色标记，控制面特权判定用。
    """

    user_id: int | None
    user_name: str
    role: str
    allowed_tools: list[str] = field(default_factory=list)
    knowledge_scope_tags: list[str] = field(default_factory=list)
    level: int = 0
    system_prompt: str = ""
    is_admin: bool = False


def get_active_system_prompt(session: Session) -> str:
    """返回当前激活的系统提示词内容。

    全局应只有一条 ``is_active=True`` 的 SystemPrompt。无激活版本时返回空串兜底
    （数据面拿到空 system_prompt 即不注入额外指令，不致错）。
    """
    stmt = SystemPrompt.select_active()
    active = session.exec(stmt).first()
    return active.content if active is not None else ""


def compute_capability_set(session: Session, user: User) -> CapabilitySet:
    """从用户记录计算当前能力集。

    - 普通用户：取 ``user.role`` 对应的 ``Role.config``（allowed_tools /
      knowledge_scope_tags / level），叠加当前激活的 SystemPrompt.content。
    - admin 内置角色：``is_admin=True``，``allowed_tools=['*']``、
      ``knowledge_scope_tags=['*']``、``level=999``（最高）。system_prompt 仍取激活版本。
    - ``is_active=False`` 用户：抛 ``PermissionError("用户已被禁用")``。
    - role 对应的 Role 记录不存在（除 admin 外）：抛 ``PermissionError("角色未定义: {role}")``。

    所有策略集中在此函数，是治理脊柱与 LLM 之间唯一的翻译层。
    """
    # 1. 先校验用户状态——禁用用户无论角色都拒绝，不泄漏后续判定细节。
    if not user.is_active:
        raise PermissionError("用户已被禁用")

    # 2. 激活系统提示词对所有角色一视同仁（M1 全公司统一一套）。
    system_prompt = get_active_system_prompt(session)

    # 3. admin 内置角色：不走 Role 表，直接给通配 + 最高职级。
    if user.role == "admin":
        return CapabilitySet(
            user_id=user.id,
            user_name=user.name,
            role=user.role,
            allowed_tools=["*"],
            knowledge_scope_tags=["*"],
            level=999,
            system_prompt=system_prompt,
            is_admin=True,
        )

    # 4. 普通用户：必须能找到对应的 Role 记录，否则视为配置错误，拒绝。
    role_record = session.exec(select(Role).where(Role.name == user.role)).first()
    if role_record is None:
        raise PermissionError(f"角色未定义: {user.role}")

    config = role_record.config
    return CapabilitySet(
        user_id=user.id,
        user_name=user.name,
        role=user.role,
        allowed_tools=list(config.allowed_tools),
        knowledge_scope_tags=list(config.knowledge_scope_tags),
        level=config.level,
        system_prompt=system_prompt,
        is_admin=False,
    )
