# Bycall 下一阶段执行计划（Execution Plan）

> 状态：**主方向（进行中：阶段 0/1/2 已完成，阶段 3 进行中——T3.1 代码与 T3.3 审批授权三档已落地）** · 更新：2026-08-21 · 关联：[决策登记册](decisions/README.md)
> 本文是 Bycall 当前唯一的**执行级工作方向**。它取代"继续按里程碑 M7+ / S3-S4 堆能力"的默认，
> 改为"先收口、再评测、后试点"。所有新任务必须对齐本文的阶段与任务编号，未列入冻结清单的新
> 能力不得动工。里程碑路线图的历史沿革仍见 `product-design.md` §6，本文与其冲突时以本文为准。

## 0. 背景与依据

2026-08-13 项目简报的诊断结论（忠实评价）——六个问题：

| # | 问题 | 严重度 | 对应阶段 |
|---|------|--------|---------|
| 1 | 治理架构漂亮但**零客户验证**，单人 + 5 周 + 无试点 | 致命 | 阶段 3 |
| 2 | 在"重造平台"赛道与 Dify/Coze/RAGFlow 竞争，功能广度必输 | 致命 | 阶段 3 |
| 3 | M5 生产化缺口 + 六条"治理承诺 vs 实际执行"缝隙 | 高 | 阶段 1 |
| 4 | 路线图持续膨胀，有范围蔓延（前端打磨替代硬骨头） | 中 | 冻结清单 |
| 5 | 缺自身评测/回放/发布门，无法证明"不越权、不回退" | 高 | 阶段 2 |
| 6 | 10 项决策悬而未决 + 文档滞后 | 中 | 阶段 0 |

**结论：12 周内，拍板 → 收口六条治理缝隙 + 补 SSO/多租户/策略模拟器 → 建评测与发布门 →
拿下一个真实试点并用数据验证卖点；其余新能力全部冻结，直到试点证明"组织授权下的可靠执行"
真的有人买单。**

## 1. 总原则（不可违背）

1. **一个主轴**：一切服务"组织授权下的可靠执行"。不做与治理/信任无关的广度功能。
2. **先收口后扩展**：治理承诺 ≠ 实际执行的缝隙（阶段 1）优先级**高于** S3/S4 新能力。
3. **一切可验证**：每个任务都带可自动化验收的判定标准（Acceptance + Verify），不留"看起来完成"。
4. **冻结非核心**：前端视觉/主题/排版打磨、对话测试页 UI 类工作**暂停**，除非试点/审计必需。
5. **减单点**：每个 P0 模块至少留 review 或对拍环节；关键决策文档化，降低 bus factor。

## 2. 阶段总览

| 阶段 | 名称 | 周 | 解决问题 | 出口门 |
|------|------|----|---------|--------|
| 0 | 决策冻结 + 文档同步 | 1 | #6 | 待拍板项清零（转已采纳或明确冻结） |
| 1 | 生产化收口 | 2–4 | #3 | 六条缝隙闭合 + 策略模拟器可演示 + CI 全绿 |
| 2 | 评测与发布系统 | 5–8 | #5 | 一条命令回答"变更是否越权/回退" |
| 3 | 试点与参考场景（行业无关，ADR-0009） | 9–12 | #1 #2 | 参考场景自助可跑 + 真实试点数据（含未授权召回=0） |

冻结清单（问题 #4）见 §4。任何阶段若出口门不达标，**停在该阶段**，不得跳到下一阶段。

---

## 3. 阶段详细任务

> 任务卡片字段：Description / Acceptance（验收）/ Verify（验证命令）/ Dependencies（依赖）/
> Files（改动文件）/ Scope（S/M/L，估计改动规模）。

### 阶段 0：决策冻结 + 文档同步（第 1 周）

#### T0.1 拍板三项关键决策
**Description**：对登记册 §3 的第 6/7/8 项（首个付费行业 / 行业包是否版本化资产 / 收口与试点资源配比）
拍板；其余 §3 待拍板项（1/2/3/5/9/10）若不影响未来 12 周执行，明确标注"后置，不阻塞本计划"。
**Acceptance**：
- [x] 首个付费行业已拍板（2026-08-15）：不锁定行业——平台场景无关（ADR-0009），以参考场景包支撑试点
- [x] 单租户优先已由 ADR-0004 拍板，不再重复讨论；本计划按"单组织私有部署"推进
- [x] 登记册 §3 每项标记为"已拍板（记录见 ADR/正文）"或"后置（不阻塞）"
**Verify**：`docs/decisions/README.md` §3 无可阻塞本计划的 open 项。
**Files**：`docs/decisions/README.md`（+ 需要时新增 ADR-0009）。**Scope**：S。

#### T0.2 冻结范围决策
**Description**：把 S3/S4、行业包资产化、前端视觉/主题/排版打磨正式移入冻结清单（§4），登记为已采纳决策。
**Acceptance**：
- [x] §4 冻结清单写入登记册 §2（"已采纳：冻结项"）
- [x] 未来 12 周任务清单不引用被冻结项
**Files**：`docs/decisions/README.md`、本文 §4。**Scope**：S。

#### T0.3 文档纠偏
**Description**：清理过时状态——`product-design.md` §6 M1 验收标准标注过时、README 里程碑 M6 表述统一、
`agent-framework-upgrade.md` 状态栏同步 S2 Phase 2 已落地。
**Acceptance**：
- [x] 文档状态栏（登记册 §5）与代码实况一致
- [x] 无"宣称已完成、实际未做"的误导性表述
**Files**：`docs/product-design.md`、`README.md`、`docs/specs/agent-framework-upgrade.md`。**Scope**：S。

#### T0.4 建立治理缝隙清单
**Description**：把战略 §3 六条缝隙落成带 owner + 目标里程碑 + 现状源码定位的清单。
**Acceptance**：
- [x] 六条缝隙逐条标注 owner + 对应阶段任务编号（T1.1–T1.6）
- [x] 每条附"当前源码位置 + 目标行为"，可直接作为阶段 1 任务输入
**Files**：`docs/gaps.md`（新）。**Scope**：S。

**阶段 0 出口门**：登记册 §3 无可阻塞项；§4 冻结清单已登记；`docs/gaps.md` 就绪。

---

### 阶段 1：生产化收口（第 2–4 周）

> 六条缝隙（战略 §3）+ M5 企业身份。这是"能否进真实生产"的门槛，也是卖点是否会反噬的关键。

#### T1.1 完整 ABAC 闭环（缝隙 #1）
**Description**：落地 ADR-0005 的"统一 ABAC Policy 接口"（当前 `governance/` 尚无 `policy.py` 类，仅
`knowledge_scope_tags → source IN (...)` 的角色标签过滤）。新增统一 policy 引擎，把
`ACLMetadata`(tenant_id/dept/level/敏感度) + 用户属性 + 资源属性 + 上下文翻译成**召回前**过滤条件。
**Acceptance**：
- [x] `governance/policy.py` 提供统一 `evaluate()`，输入（用户, 资源, 动作, 上下文）→ 过滤条件
- [x] `rag/retrieve.py` 的检索 filter 由 policy 产出，不再是手写 `source IN`
- [x] 加一条"跨部门+职级+密级"文档，验证召回前即被过滤（不进 LLM、不留痕）
**Verify**：`make test-unit` + 集成测试（越权召回=0）。
**Dependencies**：T0.4。**Files**：`governance/policy.py`（新）、`governance/capability.py`、`rag/retrieve.py`、`api/dataplane.py`。**Scope**：L。

#### T1.2 `visible_kb_ids` 语义落地（缝隙 #4）
**Description**：`CapabilitySet.visible_kb_ids` 已计算但数据面 runner 仍遍历全部 KB。二选一并落地：作为数据面
ACL 接入检索，或更名移出能力集——**禁止**让未生效字段继续被误认为安全边界。
**Acceptance**：
- [x] 若作为 ACL：检索入口只对 `visible_kb_ids` 内的 KB 召回（测试验证）
- [x] 若更名：字段改名/移除，代码与文档同步，不再产生"看似是边界"的误读
**Verify**：`make test-unit` + `make test-integration`。
**Dependencies**：T1.1。**Files**：`governance/capability.py`、`api/dataplane.py`、`agent/runner.py`。**Scope**：M。

#### T1.3 per-session 模型绑定生效（缝隙 #3）
**Description**：`Conversation.model_id` 已预留但运行时仍取全局激活模型。把模型选择纳入能力集 + 成本/驻留策略，
runner 按会话绑定模型调用。
**Acceptance**：
- [x] 会话绑定模型后，该会话走绑定模型而非全局激活（有测试锁定）
- [x] 未绑定时 fallback 全局激活（零回归）
**Verify**：`make test-integration`（多会话模型路由）。
**Dependencies**：None。**Files**：`agent/runner.py`、`llm/resolver.py`、`governance/models.py`。**Scope**：M。

#### T1.4 同步/流式流程收敛（缝隙 #6）
**Description**：`run_agent` 与 `run_agent_stream` 两套高度相似流程易漂移。抽共享的"策略准备 → 执行 → 后处理 →
审计"管线，两路共用（任务维度 event 已由 default_bus 闭合，此处闭合对话维度）。
**Acceptance**：
- [x] 两路经同一 pipeline 入口，审计字段一致（有测试锁定）
- [x] 流式逐 token 语义不破坏（现有企微流式测试绿）
**Verify**：`make ci-local` 全绿 + 新增管线单测。
**Dependencies**：None。**Files**：`agent/pipeline.py`（新）、`agent/runner.py`、`api/dataplane.py`。**Scope**：L。

#### T1.5 会话键真多租户化（缝隙 #2）
**Description**：红线定义 `(agent_id, channel, tenant_id, user_id)`，当前 DB 主要是 `(user_id, channel)`。
补显式隔离键，跨租户/跨 Agent 会话与记忆互不可见。
**Acceptance**：
- [x] 会话、记忆、审批、审计、MCP、缓存均带 tenant/agent 隔离键
- [x] 跨租户会话与记忆互不可见（测试覆盖）
**Verify**：`make test-integration`（多租户隔离）。
**Dependencies**：None（DB 迁移，遵循"高风险改动先确认"）。**Files**：`governance/models.py`、`db.py`。**Scope**：L。

#### T1.6 工具结果统一脱敏平台化（缝隙 #5）
**Description**：当前脱敏依赖单个工具声明 redactor，MCP 靠 capability meta 自行鉴权。平台化：字段分级 +
行级过滤 + 输出大小限制，**默认拒绝未声明的数据类型**。
**Acceptance**：
- [x] 统一 redactor 策略层，未声明数据类型的工具返回默认被裁剪
- [x] 输出大小限制生效（防上下文撑爆）
**Verify**：`make test-unit` + MCP 工具集成测试。
**Dependencies**：None。**Files**：`tools/redactor.py`（新）、`tools/base.py`、`tools/gateway.py`。**Scope**：M。

#### T1.7 SSO + 组织同步 + 离职撤权（M5 企业身份）——已按现实收口
**范围决策（2026-08-13）**：
- ✅ **离职即时撤权已闭环**（既有机制）：`User.is_active=False` → `compute_capability_set` 抛 PermissionError（数据面拒绝）+ 登录/act-as/IM `select_by_im`/审批执行/结果推送/任务 Step 全链路拒绝。已有 `test_capability.py` / `test_auth.py` / `test_admin_revocation.py` / `test_gateway.py` / `test_approval.py` / `test_approval_push.py` / `test_step_executor.py` 等全面覆盖，无需新代码。
- ⏸ **OIDC/SAML SSO 登录 + 组织架构同步后置**：属客户依赖（需客户 IdP / 目录，配置驱动），无客户时不可测、纯属推测性实现；待首个试点组织确定后再排期。本地账号密码登录保持现状。
**Files**：无新增（撤权已有实现 + 测试）。**Scope**：S（仅范围确认）。

#### T1.8 策略模拟器（控制面）
**Description**：管理员选（用户, 资源, 动作, 上下文）→ 系统显示最终 allow/deny + 命中规则 + 能力集 + 过滤条件。
治理结果不再靠读代码理解。
**Acceptance**：
- [x] 控制面可独立验证治理结果，与数据面实际判定一致
- [x] 前端新页面 + 后端端点 + 单测
**Verify**：`make test-unit` + 前端 `npm run build`。
**Dependencies**：T1.1。**Files**：`admin/api.py`、`frontend/src/views/`（新页面）。**Scope**：M。

#### T1.9 上下文预算与护栏（阶段 1 追加，2026-08-14 完成）
**Description**：RAG 注入统一预算化（per-chunk 截断 / 跨 KB 去重 / 低相关过滤 / 总预算 / 相关性
标注），per-role 上下文预算（``RoleConfig.context_budget``），投毒软降级 + 审计反馈环；
多轮/单轮两路共用同一管线，消除旧“裸拼接抄两份”的缝隙。
**Acceptance**：
- [x] RAG 注入有字符/条数预算且审计留痕（context_report）
- [x] 提示注入特征命中即标注降权 + 计入审计（软降级不硬删）
- [x] per-role 预算可覆盖全局默认，存量配置零迁移
**Verify**：`make ci-local` 全绿（见 `tests/unit/test_context_budget.py`、`test_prompt_injection_boundary.py`）。
**Files**：`agent/context_budget.py`、`agent/session_store.py`、`agent/runner.py`、`governance/models.py`。**Scope**：M。

**阶段 1 出口门**：`make ci-local` 全绿 + 六条缝隙全部闭合（`docs/gaps.md` 逐条勾掉）+ 策略模拟器可演示。

---

### 阶段 2：评测与发布系统（第 5–8 周）✅ 已完成（2026-08-15）

> 以"治理安全"为卖点，必须能**自动证明不越权、不回退**。
> 实现决策见 ADR-0010；引擎 `src/bycall/evals/`，仓库入口 `evals/run.py`，
> `make ci-local` 已含 `eval` + `eval-security`。

#### T2.1 四类评测集骨架 + runner
**Description**：知识质量 / 权限安全 / 执行质量 / 运营效果四类评测集骨架 + 可运行 runner。
**Acceptance**：
- [x] 每类至少一个可运行样例（`make eval` 或单独入口）
- [x] 评测结果可量化输出（分数/通过率）
**Verify**：新增 `make eval` 目标可跑。
**Dependencies**：阶段 1 出口门。**Files**：`evals/`（新目录 + runner）、`src/bycall/evals/`。**Scope**：M。

#### T2.2 权限攻击用例集（红线级）
**Description**：越权召回、跨用户记忆泄漏、提示词注入、工具越权、审批绕过五类攻击用例。
**Acceptance**：
- [x] 每类至少 3 条用例，全部进入 CI（或独立 `make eval-security`）
- [x] 任一用例失败即红灯
**Verify**：`make eval-security` 全绿。
**Dependencies**：T2.1。**Files**：`evals/security/`（`src/bycall/evals/cases_security.py`）。**Scope**：M。

#### T2.3 离线回放 + 新旧对比
**Description**：Prompt/Skill/模型/检索配置变更可离线回放，输出新旧 diff。
**Acceptance**：
- [x] 变更前自动跑评测集，输出 diff
- [x] 支持按变更类型选择对应评测子集
**Dependencies**：T2.1。**Files**：`evals/` + 配置版本快照。**Scope**：M。

#### T2.4 发布门 + 灰度 + 回滚
**Description**：评测不通过禁止发布；支持灰度与一键回滚。
**Acceptance**：
- [x] 一个失败用例即可阻断对应配置发布
- [x] 回滚 = 切换 is_active 到旧版本（复用现有 version + is_active 模式）
**Dependencies**：T2.3。**Files**：`admin/api.py`、发布流程脚本、`governance/release.py` + `ReleaseState` 表。**Scope**：M。

#### T2.5 运营指标埋点
**Description**：延迟/成本/人工接管率/失败率/重复执行率五维指标。
**Acceptance**：
- [x] 复用 OTel/Prometheus 产出五维指标
- [x] 控制面看板可见（可作为试点阶段 D4 的底子）
**Verify**：`make dev` 后 `/metrics` 含目标指标。
**Dependencies**：None。**Files**：`observability/metrics.py`、`observability/usage.py`、前端看板（运营指标页）。**Scope**：S。

**阶段 2 出口门**：一条命令回答"这次改 prompt 有没有造成越权或质量回退"。

```
# 出口门命令（快照 + 回放，任一新配置用例失败 → 退出码 1）
python -m evals.run --snapshot evals/snapshots/old.json
# ...修改配置...
python -m evals.run --snapshot evals/snapshots/new.json
python -m evals.run --compare evals/snapshots/old.json evals/snapshots/new.json
```

---

### 阶段 3：试点与参考场景（第 9–12 周，行业无关——见 ADR-0009）

> 唯一能证明项目价值的环节。优先级等同甚至高于阶段 2——试点接触应从阶段 0 即启动，不拖到最后。
> 按 ADR-0009（2026-08-15 拍板）：平台**场景无关、不锁定行业**，垂直场景由采用者自定义；
> 本阶段交付「参考场景包」+ 不限行业的真实试点接触。

#### T3.1 参考场景包 + 试点接触（行业无关）——已完成代码部分（2026-08-15）
**Description**：按 ADR-0009 重定义——不锁定单一行业试点。交付参考场景包：2 个示例 MCP
连接器（工单 + 库存，`examples/connectors/`）+ 3 类示例角色（一线/主管/访客）+ demo 账号
+ 一键种子 `make seed-reference`（幂等）。真实试点接触不限行业、商务并行推进（自阶段 0 已启动）。
**Acceptance**：
- [x] 任何采用者 `make seed-reference` 后参考场景端到端可跑（demo 账号体验能力差异）
- [x] 连接器经 MCP 网关注册（红线 #5）；高危工具 `destructiveHint` → 审批门自动拦截
- [ ] 真实试点组织锁定（商务项，行业不限；不阻塞 T3.2 参考流程开发）
**Verify**：`make seed-reference` + `make test-unit`（reference 用例）。**Dependencies**：阶段 1
出口门（已达标）。**Files**：`examples/connectors/`、`scripts/seed_reference.py`、Makefile。**Scope**：M。

#### T3.2 三条端到端参考流程
**Description**：故障诊断 / 备件查询并建工单 / 高风险操作审批回推（**参考场景**，采用者按
ADR-0009 替换为自己的流程；不再绑定具体行业）。
**Acceptance**：
- [ ] 每条流程 IM 里端到端走通（含 RAG + 工具 + 推送）
- [ ] 复用 T2.2 权限用例对参考场景配置做安全回归
**Dependencies**：T3.1。**Files**：流程配置包（角色+提示词+Skill+知识库结构+连接器）。**Scope**：L。

#### T3.3 一条高风险 HITL 审批链 —— ✅ 已完成（2026-08-21，ADR-0011）
**Description**：停机/参数修改等操作必须人审，全程可追溯；批准时可按档位授权
（once/session/always），授权与撤销全程落库可审计。
**Acceptance**：
- [x] 审批三档（once/session/always）持久化（`ApprovalGrant` 表 + 审批门短路 + 台账）
- [x] 审批结果主动推送发起员工，全程审计可追溯（M6 推送已有；免审执行亦入台账
  `grant_id` + `reviewed_by=grant:*`；新增 3 条授权越权安全用例锁定边界）
**Dependencies**：T3.1。**Files**：`governance/models.py`（ApprovalGrant）、
`tools/approval.py`（短路/台账）、`admin/api.py`（grants 端点）、
`views/Approvals.vue`（档位 + 授权管理）。**Scope**：M。

#### T3.4 业务效果看板
**Description**：首修率 / 故障处理时间 / 工单成功率 / 人工接管率。
**Acceptance**：
- [ ] 基线 vs 使用后数据对比
- [ ] 复用 T2.5 指标，不另造埋点
**Dependencies**：T3.2、T2.5。**Files**：前端看板。**Scope**：M。

**阶段 3 出口门**：拿到真实数据——**未授权召回=0、引用覆盖率、工具参数正确率、任务成功率、人工接管率**。
据此决定继续投入或止损转向（止损也是计划的一部分，不是失败）。

---

## 4. 冻结清单（问题 #4）

| 项目 | 状态 | 恢复条件 |
|---|---|---|
| S3 skill 演化 / S4 分发 | 冻结 | 阶段 1+2 完成，试点证明记忆/执行链路稳定后 |
| 行业包版本化/导入导出 | 冻结 | 阶段 3 跑通 ≥1 行业后 |
| 前端视觉/主题/排版打磨 | 冻结 | 试点或客户明确提出需求时 |
| 成本预算 / 推送 outbox 完善 | 冻结 | 多用户真实流量出现后再排 |
| 多 Agent 群聊、低代码画布、Skill 公共市场 | 不做 | 战略 §7 已明确，维持 |
| QM 群聊共同授权（audience floor） | 冻结 | 有群聊场景的真实客户需求时 |

## 5. 风险与依赖

| 风险 | 影响 | 缓解 |
|------|------|------|
| 单人 12 周排满易延期 | 高 | 阶段 1/2/3 只保留 P0 任务；每两周砍范围评审 |
| 试点组织难找 | 高 | 阶段 0 拍板行业后立即启动接触，不拖到阶段 3 |
| 治理卖点未验证 | 高 | 阶段 3 数据不达预期即止损，明确写进出口门 |
| 六条缝隙牵一发动全身 | 中 | 按 T1.1→T1.2→…→T1.6 顺序，先做有测试锚点的项 |
| DB 迁移/多租户改动风险 | 中 | 遵循"高风险改动先停下确认"，迁移前备份 |

## 6. 与现有文档的关系

- 本文是**执行主方向**，取代默认的"继续 M7+ / S3-S4 堆能力"。
- 里程碑历史沿革见 `product-design.md` §6；架构红线（`architecture.md`）不变，仍是硬约束。
- 任务执行时仍遵守 `AGENTS.md`（codegraph 先探索、worktree、中文 commit、CHANGELOG 强制）。
- 本文的登记状态见 `docs/decisions/README.md` §2 / §5；六条缝隙明细见 `docs/gaps.md`（阶段 0 产出）。