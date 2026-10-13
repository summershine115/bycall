"""统一 ABAC 策略接口（ADR-0005）。

把「主体属性 + 资源属性 + 上下文」翻译成召回前的过滤条件。策略活在代码层，
检索层只消费 policy 产物，不再手写 ``source IN``——这是治理脊柱与检索层之间
唯一的翻译层，与 ``capability.py``（用户+角色→能力集）对偶：
capability 产出「谁能做什么」，policy 产出「该能力对应哪些过滤条件」。

当前规则（fail-closed，红线 #1 权限预过滤）：
- 知识域白名单：``knowledge_scope_tags`` → ``source IN (...)``。
- 密级/职级：``resource_level <= user_level`` → ``level <= N``（机密文档对低职级用户
  召回前即被过滤，不进 LLM、不留痕）。
- admin 或通配 ``'*'`` → 空过滤（全可见）。

dept/sensitivity 预留：zvec schema 已建字段，但 User 尚无 dept 属性，
待数据模型补齐后在 ``build_acl_filters`` 追加 ``dept`` 条件（见 doc/gaps.md #1 剩余项）。

设计约束：本模块不访问 DB（与 capability 的计算分离），只做纯函数式过滤条件构造；
输入由调用方从 CapabilitySet 裁剪成 PolicySubject 传入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bycall.vector_store.models import FilterCondition


@dataclass
class PolicySubject:
    """一次检索的主体属性——ABAC 判定的唯一输入。

    与 ``CapabilitySet`` 的区别：CapabilitySet 是完整能力集（含提示词/skill 等），
    PolicySubject 只裁剪出 ABAC 判定需要的字段，policy 不依赖其余，避免耦合。
    """

    is_admin: bool = False
    knowledge_scope_tags: list[str] = field(default_factory=list)
    level: int = 0  # 职级，越大权限越高


def subject_from_capability(cap) -> PolicySubject:
    """CapabilitySet → PolicySubject（从能力集裁剪 ABAC 判定所需字段）。

    局部导入避免与 capability 模块的循环依赖。
    """
    return PolicySubject(
        is_admin=cap.is_admin,
        knowledge_scope_tags=list(cap.knowledge_scope_tags),
        level=cap.level,
    )


def build_acl_filters(subject: PolicySubject) -> list[FilterCondition]:
    """由主体属性构造召回前过滤条件（权限预过滤，红线 #1）。

    - admin 或通配 ``'*'`` → 空列表（不过滤，全可见）。
    - 否则 → ``source IN tags``（知识域白名单）+ ``level <= subject.level``（密级/职级）。
      密级过滤恒加：即使用户职级为 0，也发出 ``level <= 0``，确保机密文档（level>=1）
      被 fail-closed 地过滤掉，而非因「没发条件」而泄漏。

    空 tags 的隔离由调用方短路（retrieve 层 ``if not tags: return []``），
    本函数只负责「有权限时」的条件构造。
    """
    if subject.is_admin or "*" in subject.knowledge_scope_tags:
        return []

    filters: list[FilterCondition] = []
    if subject.knowledge_scope_tags:
        filters.append(
            FilterCondition(field="source", op="in", values=subject.knowledge_scope_tags)
        )
    filters.append(FilterCondition(field="level", op="lte", value=subject.level))
    return filters
