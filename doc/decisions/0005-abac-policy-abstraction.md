# ADR-0005：抽象统一 ABAC Policy 接口（自研，暂不引 OPA/Cedar）

- **状态**：已采纳
- **日期**：2026-07-30（M5）
- **背景**：ABAC 数据结构已预留但检索路径未消费
- **关联**：`architecture.md` §架构红线 #1、`product-design.md` §支柱 2/4、
  `product-strategy.md` §3 缝隙 #1/#4、§8 决策点 3

## 背景

ABAC 的「数据结构层」已就位，但「翻译层」缺失：

- `CapabilitySet.level` 预留了 ABAC（注释：「职级，ABAC 用，机密资源按
  `resource_level <= user_level` 放行」），但检索路径不消费它
  （`src/bycall/governance/capability.py`）。
- chunk 的 `ACLMetadata` 预留了 `dept / level / acl_users / acl_groups /
  sensitivity_label` 等属性字段（`src/bycall/vector_store/models.py`），但
  `_retrieve_across_kbs(session, question, knowledge_scope_tags)` 实际只把
  `knowledge_scope_tags` 翻译成 `source IN (...)` 一种过滤
  （`src/bycall/agent/runner.py`）。
- 附带歧义：`CapabilitySet.visible_kb_ids` 已计算（注释称「admin 控制面 KB 列表过滤」），
  但 `_retrieve_across_kbs` 遍历全部 KB——该字段到底代表数据面 ACL 还是控制面管理范围，
  语义未统一（`product-strategy.md` §3 缝隙 #4）。

即：红线 #1（权限预过滤）目前只落到「source 标签过滤」，ABAC 的完整属性集尚未闭环
（`product-strategy.md` §3 缝隙 #1）。

## 决策

**自研统一 Policy 抽象**，把「用户属性 × 资源属性 × 上下文 → 召回前 metadata filter」的
翻译收敛到一个接口（建议 `src/bycall/governance/policy.py`），替代当前散落在
`_retrieve_across_kbs` 里的 `source IN (...)` 直翻：

1. 让 `level / dept / sensitivity_label / acl_users / acl_groups` 等预留属性都能进入召回前
   filter（如机密资源按 `resource_level <= user_level` 放行）。
2. `run_agent` 与 `run_agent_stream` 两套路径共用同一策略翻译（为 §3 缝隙 #6 的同步/流式
   收敛铺路）。
3. 在此过程中**厘清 `visible_kb_ids` 语义**：若代表数据面 ACL 则接入检索；若仅代表控制面
   管理范围则移出能力集或更名，消除歧义。
4. **暂不引入** OPA / Cedar 等外部策略引擎。

## 为什么不引 OPA/Cedar

关键事实：Bycall 的权限判定要**翻译成召回前 metadata filter**（向量检索前注入），不是只
给「允许 / 拒绝」布尔决策。OPA（Rego）/ Cedar 输出布尔决策后，还得自写一层「决策结果 →
filter 条件」的翻译——引入外部引擎的边际收益被这层翻译抵消：

- 当前规则简单（source 标签 + level 比较），自研可控、零新依赖、与 `CapabilitySet` /
  SQLModel 无缝。
- 引入外部引擎的固定成本：新部署组件 / 学习曲线 / 属性同步（把用户与资源属性喂进引擎）。

## 何时重新评估（任一满足）

- 规则组合爆炸（部门 × 职级 × 项目 × 地域 × 密级），自研翻译难维护。
- 需要审计决策日志（谁命中哪条规则）、动态规则热更新、多系统共享策略。
- 重新评估时优先看 **Cedar**（专为授权 / 数据场景设计，比 Rego 的通用逻辑编程更贴 ABAC）。

## 后果

**优点**：
- ABAC 真正闭环：预留字段从「死字段」变成被消费的约束，红线 #1 从「只过滤 source」升级为
  「过滤完整属性集」。
- 收敛散落逻辑：filter 翻译集中到一处，两套 run 路径共用。
- `visible_kb_ids` 歧义消除。

**代价**：
- 多一层抽象；过渡期须保证**零回归**（无 level/dept 配置时，filter 退化为当前 source-tag
  过滤）。
- 不引外部引擎 = 复杂规则的可维护性靠自研接口的克制设计兜底（YAGNI）。

## 约束

- **默认行为零回归**：无 level/dept 配置时，filter 退化为当前 `source IN (...)`。
- filter 始终是**召回前注入**（红线 #1），不是先召回再过滤。
- policy 接口必须**可单测**：给定用户属性 + 资源属性，断言生成的 filter 条件。

## 相关

- 治理源头：`src/bycall/governance/capability.py`（`CapabilitySet`：`knowledge_scope_tags` /
  `level` / `visible_kb_ids`）
- 当前翻译点：`src/bycall/agent/runner.py`（`_retrieve_across_kbs`）
- chunk ACL：`src/bycall/vector_store/models.py`（`ACLMetadata`）
- 检索请求：`src/bycall/vector_store/models.py`（`SearchRequest.filters`）
- 红线：`architecture.md` §架构红线 #1
- 缝隙：`product-strategy.md` §3 缝隙 #1/#4、§8 决策点 3
