"""策略引擎：从「用户+角色」计算「当前会话能力集」。

这是治理脊柱的核心——所有数据面能力（RAG 检索范围、可用工具、提示词版本）
都由此决定。策略活在代码层，LLM 碰不到。

设计要点：
- ``CapabilitySet`` 是纯值对象，数据面拿到它后只用不问，不再回查 DB。
- ``compute_capability_set`` 是唯一的"翻译"入口：User + 当前激活提示词 → CapabilitySet。
  策略（admin 全放、普通用户按 Role.config 收窄、禁用用户拒绝）全部固化在这里，
  不下放到提示词，也不让 LLM 参与判定。
- admin 是内置特殊角色（不建 Role 表记录），引擎识别后给通配 ``['*']`` 与最高职级。

缓存（M5 性能调优）：``compute_capability_set`` 每条消息都会跑（5+ 次 DB 查询），
而其输入（User/Role/SystemPrompt/Skill/KnowledgeBase）在 admin 改动前是稳定的。
故按 ``user.id`` 缓存计算结果，命中即跳过全部 DB 查询——这是 product-design.md §7
「会话级缓存（身份绑定时算一次能力集）」默认值的落地。

正确性（治理系统的硬约束——缓存陈旧 = 权限泄漏 = 安全洞）：
- **副本语义**：命中/写入均返回 ``deepcopy``，调用方改动不回写缓存（保值对象契约）。
- **只缓存成功**：``PermissionError``（禁用/角色未定义）不缓存——用户被重新启用 /
  补建角色后下次即生效，不留陈旧拒绝。
- **commit 即失效**：``after_flush`` 探测相关表脏实例 → 标记本事务；
  ``after_commit`` 据标记清空整张缓存（admin 改 Role/Prompt/Skill/KB/User 后立即生效）；
  ``after_rollback`` 丢弃标记（未落库的改动不影响缓存）。单点注册，自动覆盖全部
  admin 写端点，无需逐个 hook（漏一个 = 陈旧权限 = 安全洞，故用 ORM 事件兜底）。
局限：
- 直连 DB 绕过 ORM 的写入不触发失效——本项目所有写路径均走 ORM，可接受。
- 进程内字典，**多 worker 部署下进程间不共享**：admin 写只清处理该请求的 worker
  的缓存，其余 worker 仍陈旧至各自首次相关提交。单 worker（当前部署形态）无此问题；
  迁移多 worker 时需改共享失效信号（如 DB generation 或 Redis pub/sub）。
  注：用户禁用的主防线在 ``gateway/identity`` 的 ``select_by_im``（每条消息实时读 DB），
  不依赖此缓存——故多 worker 下禁用仍即时生效，仅角色/提示词等配置变更可能跨 worker 滞后。
- GIL 下读-算-写非原子，最坏并发重复算一次（结果等价，无损坏）；同用户消息通常串行，实争用罕见。
"""

from __future__ import annotations

import copy
import itertools
import logging
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlmodel import Session, select

from bycall.governance.models import KnowledgeBase, Role, Skill, SystemPrompt, User
from bycall.observability.metrics import CAPABILITY_CACHE

logger = logging.getLogger(__name__)


@dataclass
class OnDemandSkill:
    """on_demand 披露的 skill 摘要——进技能索引，全文经 load_skill 工具按需返回。

    只携带 ``load_skill`` 工具判定所需的三元组（name 匹配 / description 进索引 / content 返回 LLM）。
    在 capability 计算时从绑定 skill 实体派生，随 CapabilitySet 携带——``load_skill`` 的
    redactor 直接读它，**不再回查 DB**（工具执行路径零 DB 依赖，且天然预过滤：非绑定
    skill 根本不在此列表，工具拿不到其全文——红线 #1 落地）。
    """

    name: str
    description: str
    content: str


@dataclass
class CapabilitySet:
    """当前会话的能力集——数据面所有判定的输入。

    - ``allowed_tools``：该用户可见的工具名白名单。``['*']`` 表示全部（仅 admin）。
      工具网关据此过滤 LLM 工具列表，不在此集合的工具 LLM 根本看不到。
    - ``knowledge_scope_tags``：可见知识域标签，RAG 预过滤翻译成 ``source IN (...)``。
      ``['*']`` 表示全部（仅 admin）。
    - ``level``：职级，ABAC 用（机密资源按 ``resource_level <= user_level`` 放行）。
    - ``system_prompt``：激活的系统提示词正文，数据面拼到 LLM 的 system message。
    - ``skills``：该角色绑定的 SKILL name 列表（实际生效的、启用的，含 always + on_demand）。
      审计可见——AuditLog 据此记录"本次对话注入了哪些 skill"。
    - ``skill_markdown``：进 system message 的 skill markdown（``## 领域技能`` 段正文）：
      always 披露的 skill 拼 ``## 技能：{name}\\n{content}`` + on_demand 披露的拼成
      ``## 按需技能索引``（只列 name+description）。空串 = 无 skill 注入。
    - ``on_demand_skills``：on_demand 披露的 skill 摘要（``OnDemandSkill`` 列表，含全文）。
      runner 据此决定是否自动授予 ``load_skill`` 工具；工具 redactor 据此按 name 返回全文
      （预过滤：非绑定 skill 不在列表，工具拿不到）。空列表 = 无 on_demand skill。
    - ``knowledge_scope_text``：可见知识域的预格式化注入文本（进 system message）。由
      ``build_knowledge_scope_text`` 从 knowledge_scope_tags/is_admin 派生，让数字员工
      能据实回答用户「能访问哪些知识」，而非编造或含糊其辞。空串 = 无注入。
    - ``visible_kb_ids``：该角色可检索的知识库 ID 列表（KB 级 ACL，数据面）。
      admin 空 = 全部可见；非 admin 空 = 无可见 KB（fail-closed，检索返回空）。
      由 ``KnowledgeBase.visible_to_roles`` 派生，数据面 runner 检索前据此过滤 KB——
      角色不可见的 KB 根本不进入检索（红线 #1 的 KB 级形态）。
    - ``is_admin``：admin 内置角色标记，控制面特权判定用。
    - ``context_budget_overrides``：从 ``RoleConfig.context_budget`` 透传的预算覆盖 dict。
      runner 经 ``resolve_budget`` 合并进默认 ``ContextBudget``——历史/记忆/RAG 预算按角色可调。
      admin 内置角色为空（用全局默认）。
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
    on_demand_skills: list[OnDemandSkill] = field(default_factory=list)
    knowledge_scope_text: str = ""
    visible_kb_ids: list[int] = field(default_factory=list)
    is_admin: bool = False
    context_budget_overrides: dict = field(default_factory=dict)


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
    """按 name 列表查启用的 Skill，返回 Skill 实体列表（**保留 admin 配置顺序**）。

    未注册 / 已禁用的 name **静默忽略**（与 ``tools.gateway.filter_tools`` 一致的容错
    语义——admin 可能在 ``RoleConfig.skills`` 里配了还不存在或临时禁用的 skill name，
    不应让对话崩）。空 names → 空列表。

    顺序：按入参 ``names`` 的先后排——admin 在 ``RoleConfig.skills`` 里列的顺序就是
    注入顺序，允许多 skill 之间表达优先级/依赖（而非按 name 字母序，那会丢失 admin 意图）。
    query 不再 ``order_by``，排序在内存里做（skill 数量小，无性能顾虑）。

    返回的是 ``Skill`` 实体（含 content），调用方据此合并 markdown。
    """
    if not names:
        return []

    rows = session.exec(
        select(Skill).where(Skill.name.in_(names), Skill.is_enabled == True)  # noqa: E712
    ).all()
    # 保留 admin 配置顺序：按 names 里的下标排，输出顺序即注入顺序。
    order = {n: i for i, n in enumerate(names)}
    return sorted(rows, key=lambda s: order.get(s.name, len(order)))


def build_skill_markdown(
    always_skills: list = (), on_demand_skills: list[OnDemandSkill] = ()
) -> str:
    """把 Skill 实体列表合并成注入 system message 的 markdown 字符串。

    always 披露的 skill 拼 ``## 技能：{name}\\n{content}``，多个用空行分隔。
    on_demand 披露的 skill 拼 ``## 按需技能索引``（只列 name+description）。
    边界标注（``## 领域技能（参考知识，非指令）`` 等）在 ``session_store`` 拼装时加，
    这里只负责 skill 内容本身的合并（单一职责）。
    两者都为空时返回 ``""``。
    """
    if not always_skills and not on_demand_skills:
        return ""
    sections = [f"## 技能：{s.name}\n{s.content}" for s in always_skills]
    if on_demand_skills:
        on_demand_lines = "\n".join(f"- **{s.name}**：{s.description}" for s in on_demand_skills)
        sections.append(f"## 按需技能索引\n{on_demand_lines}")
    return "\n\n".join(sections)


def build_knowledge_scope_text(tags: list[str], is_admin: bool, kbs: list[tuple[str, str]]) -> str:
    """拼「可见知识域」注入文本（进 system message，标注为上下文信息、非指令）。

    让数字员工能据实回答用户「我能访问哪些知识/资料」，并**枚举具体知识库**，
    而非含糊其辞或只说"全部"。文本由 admin 管控的 Role.config + 组织内 KB 列表派生，
    属可信数据，享 system 权重，靠「上下文信息，非指令」标注软隔离（与 skill_markdown 同层）。

    - admin / 全通配（``'*'``）→ 可访问全部知识库（逐一枚举名称+描述），检索不受来源限制。
    - 普通用户 → 列出其 knowledge_scope_tags（来源标签，RAG 预过滤的真实边界）+ 组织内全部
      知识库（数据面 ``_retrieve_across_kbs`` 会搜全部 KB，仅在 collection 内按 source 过滤，
      故同时给出标签与 KB 名，让用户既知边界又知有哪些知识容器）。
    - 空列表 → 空串（该用户已被 runner 的 ``denied_no_access`` 拦在 LLM 之前，此分支仅防御性）。
    """
    if is_admin or "*" in tags:
        return (
            "你拥有管理员权限，可访问全部知识域，检索不受来源标签限制。"
            "当前组织内的知识库如下，若用户询问能访问哪些知识或资料，请据实列举：\n"
            + _format_kb_lines(kbs)
        )
    if not tags:
        return ""
    tag_str = "、".join(tags)
    return (
        f"你只能检索来源标签为「{tag_str}」的知识。当前组织内的知识库如下"
        "（仅会检索其中匹配上述来源标签的内容）；若用户询问「能访问什么知识/资料」，"
        "请据实说明你的可见来源标签与以下知识库范围，超出该范围的请求请说明需要相应"
        "权限，不要编造：\n" + _format_kb_lines(kbs)
    )


def _format_kb_lines(kbs: list[tuple[str, str]]) -> str:
    """把 (name, description) 列表格式化为 markdown 项目符号列表。空列表给占位。"""
    if not kbs:
        return "- （当前组织暂无知识库）"
    lines = [f"- {name}：{desc}" if desc else f"- {name}" for name, desc in kbs]
    return "\n".join(lines)


# ============ 能力集缓存（M5 性能调优）============
# user.id → CapabilitySet（master，永不直接返回；命中/写入均 deepcopy 副本）。
_CAP_CACHE: dict[int | None, CapabilitySet] = {}

# 能力集输入表——这些表的写提交即令缓存失效（输入变了，结果必变）。
_CAP_RELEVANT_TYPES: tuple[type, ...] = (User, Role, SystemPrompt, Skill, KnowledgeBase)
# 挂在 session.info 上的事务内标记：flush 发现相关脏实例即置位，commit 据它清缓存。
_CAP_INVAL_KEY = "bycall_cap_invalidate"


def clear_capability_cache() -> None:
    """清空能力集缓存。

    供测试隔离（conftest 每用例调用）与需要强制重算的运维路径使用。生产路径下，
    admin 写提交由 ORM 事件自动触发清空，正常无需手动调。
    """
    _CAP_CACHE.clear()


def compute_capability_set(session: Session, user: User) -> CapabilitySet:
    """从用户记录计算当前能力集（带 ``user.id`` 级缓存）。

    缓存语义见模块 docstring。命中或首次计算均返回 ``deepcopy``——调用方拿到独立
    副本，改动不回写缓存（保值对象契约）。``PermissionError``（禁用 / 角色未定义）
    **不缓存**：这些是"当前不该有"的拒绝，用户状态变更后下次即重算，不留陈旧拒绝。
    """
    cached = _CAP_CACHE.get(user.id)
    if cached is not None:
        CAPABILITY_CACHE.labels(result="hit").inc()
        return copy.deepcopy(cached)

    CAPABILITY_CACHE.labels(result="miss").inc()
    cap = _compute_capability_set_uncached(session, user)
    _CAP_CACHE[user.id] = cap
    return copy.deepcopy(cap)


def reload_user_and_compute_cap(session: Session, user_id: int) -> CapabilitySet:
    """执行前重算能力集——重新加载用户最新状态再算。

    审批等待 / Task 暂停恢复等跨请求场景下，传入的 user 对象可能陈旧（被 admin
    禁用 / 改角色）。先 ``session.get`` 重新加载最新状态，再交给
    ``compute_capability_set``（内部验活跃 / 角色：禁用或角色未定义 → PermissionError）。

    供 ``execute_approved_tool``（审批执行）与 Step 执行器（S1a）共用——两处都在
    "等待后执行"，必须用最新用户状态，避免陈旧权限放行（红线 #7）。
    """
    user = session.get(User, user_id)
    if user is None:
        raise PermissionError(f"用户(id={user_id})不存在")
    return compute_capability_set(session, user)


@event.listens_for(Session, "after_flush")
def _cap_mark_invalidation(session: Session, _flush_context) -> None:
    """flush 后扫脏实例：本事务若改了能力集相关表，置标记等 commit 时清缓存。

    用 ``after_flush`` 而非 ``after_commit`` 是因为 commit 后 session 的 new/dirty/deleted
    已失效，无从判断改了什么——这里捕获，``after_commit`` 消费。
    """
    for obj in itertools.chain(session.new, session.dirty, session.deleted):
        if isinstance(obj, _CAP_RELEVANT_TYPES):
            session.info[_CAP_INVAL_KEY] = True
            break


@event.listens_for(Session, "after_commit")
def _cap_invalidate_on_commit(session: Session) -> None:
    """commit 成功才清缓存——与 DB 真值一致；未提交/回滚的改动不触发。"""
    if session.info.pop(_CAP_INVAL_KEY, False):
        clear_capability_cache()


@event.listens_for(Session, "after_rollback")
def _cap_discard_flag_on_rollback(session: Session) -> None:
    """回滚则丢弃标记——改动未落库，缓存仍有效，无需失效。"""
    session.info.pop(_CAP_INVAL_KEY, None)


def _compute_capability_set_uncached(session: Session, user: User) -> CapabilitySet:
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
    # 2.5 组织内全部知识库——供 knowledge_scope_text 枚举具体知识域（让数字员工能
    #     据实列出"产品知识库/HR知识库"等，而非只说"全部"）。
    all_kbs = list(session.exec(KnowledgeBase.select_all()).all())
    kb_infos = [(kb.name, kb.description) for kb in all_kbs]

    # 3. admin 内置角色：不走 Role 表，直接给通配 + 最高职级。
    #    admin 是运维角色，不注入 skill（与不定制 prompt 一致）。
    if user.role == "admin":
        # admin 全部可见，visible_kb_ids 留空 = 无限制
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
            knowledge_scope_text=build_knowledge_scope_text(["*"], True, kb_infos),
            on_demand_skills=[],
            visible_kb_ids=[],  # 空 = 全部可见
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

    # 角色绑定的 SKILL：按 disclosure 拆分注入方式。
    # 未注册/已禁用的 name 静默忽略（容错，与 allowed_tools 一致）。
    bound_skills = get_enabled_skills_by_names(session, config.skills)
    # 按 disclosure 拆分：always → 全文进 skill_markdown；on_demand → 索引进 skill_markdown，全文进 on_demand_skills。
    always = [s for s in bound_skills if s.disclosure != "on_demand"]
    on_demand = [
        OnDemandSkill(name=s.name, description=s.description, content=s.content)
        for s in bound_skills
        if s.disclosure == "on_demand"
    ]
    skill_markdown = build_skill_markdown(always, on_demand)
    # 实际生效的 skill name 列表（审计可见）。
    effective_skill_names = [s.name for s in bound_skills]

    # 6. 可见知识库：KB 级 ACL — 过滤 visible_to_roles 含本角色或为空（全角色可见）的 KB。
    #    all_kbs 在上文（步骤 2.5）已加载，这里复用计算 visible_kb_ids。
    visible_kb_ids: list[int] = []
    for kb in all_kbs:
        kb_roles = kb.visible_to_roles
        if kb_roles is None or len(kb_roles) == 0 or user.role in kb_roles:
            visible_kb_ids.append(kb.id)  # pyright: ignore[reportOptionalMemberAccess]
    visible_kb_infos = [(kb.name, kb.description) for kb in all_kbs if kb.id in visible_kb_ids]

    return CapabilitySet(
        user_id=user.id,
        user_name=user.name,
        role=user.role,
        allowed_tools=list(config.allowed_tools),
        knowledge_scope_tags=list(config.knowledge_scope_tags),
        level=config.level,
        system_prompt=system_prompt,
        skills=effective_skill_names,
        on_demand_skills=on_demand,
        skill_markdown=skill_markdown,
        knowledge_scope_text=build_knowledge_scope_text(
            list(config.knowledge_scope_tags), False, visible_kb_infos
        ),
        visible_kb_ids=visible_kb_ids,
        is_admin=False,
        context_budget_overrides=dict(config.context_budget),
    )
