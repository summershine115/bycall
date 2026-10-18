# Bycall 下一阶段执行计划（Execution Plan）

> 状态：**主方向（进行中：阶段 0 已完成、阶段 1 按单组织范围收口、阶段 2 已完成，阶段 3 进行中——T3.1 代码、T3.2 自动化、T3.3 审批授权三档、T3.5 多 Bot 渠道、T3.6 面板重组、T3.7 导航精简合并已落地；真机企微演示与真实使用数据待用户）** · 更新：2026-10-13（按代码核对交付范围；文档统一到 doc/） · 关联：[决策登记册](decisions/README.md)
> 本文是 Bycall 当前唯一的**执行级工作方向**。它取代"继续按里程碑 M7+ / S3-S4 堆能力"的默认，
> 改为"先收口、再评测、后验证"。所有新任务必须对齐本文的阶段与任务编号，未列入冻结清单的新
> 能力不得动工。里程碑路线图的历史沿革仍见 `product-design.md` §6，本文与其冲突时以本文为准。

## 0. 背景与依据

2026-08-13 项目简报的诊断结论（忠实评价）——六个问题：

| # | 问题 | 严重度 | 对应阶段 |
|---|------|--------|---------|
| 1 | 治理架构完整但**零真实使用验证**，单人 + 5 周开发 | 致命 | 阶段 3 |
| 2 | 若走"重造通用平台"路线，功能广度无法与 Dify/Coze/RAGFlow 等成熟产品相比 | 致命 | 阶段 3 |
| 3 | M5 生产化缺口 + 六条"治理承诺 vs 实际执行"缝隙 | 高 | 阶段 1 |
| 4 | 路线图持续膨胀，有范围蔓延（前端打磨替代硬骨头） | 中 | 冻结清单 |
| 5 | 缺自身评测/回放/发布门，无法证明"不越权、不回退" | 高 | 阶段 2 |
| 6 | 10 项决策悬而未决 + 文档滞后 | 中 | 阶段 0 |

**结论：12 周内，拍板 → 收口六条治理缝隙 + 补 SSO/多租户/策略模拟器 → 建评测与发布门 →
以参考场景与真实使用数据验证平台；其余新能力全部冻结，直到真实使用证明"组织授权下的可靠执行"
稳定可靠。**

## 1. 总原则（不可违背）

1. **一个主轴**：一切服务"组织授权下的可靠执行"。不做与治理/信任无关的广度功能。
2. **先收口后扩展**：治理承诺 ≠ 实际执行的缝隙（阶段 1）优先级**高于** S3/S4 新能力。
3. **一切可验证**：每个任务都带可自动化验收的判定标准（Acceptance + Verify），不留"看起来完成"。
4. **冻结非核心**：前端视觉/主题/排版打磨、对话测试页 UI 类工作**暂停**，除非真实使用/审计必需。
5. **减单点**：每个 P0 模块至少留 review 或对拍环节；关键决策文档化，降低 bus factor。

## 2. 阶段总览

| 阶段 | 名称 | 周 | 解决问题 | 出口门 |
|------|------|----|---------|--------|
| 0 | 决策冻结 + 文档同步 | 1 | #6 | 待拍板项清零（转已采纳或明确冻结） |
| 1 | 生产化收口 | 2–4 | #3 | 六条缝隙闭合 + 策略模拟器可演示 + CI 全绿 |
| 2 | 评测与发布系统 | 5–8 | #5 | 一条命令回答"变更是否越权/回退" |
| 3 | 参考场景与真实使用验证（行业无关，ADR-0009） | 9–12 | #1 #2 | 参考场景自助可跑 + 真实使用验证数据（含未授权召回=0） |

冻结清单（问题 #4）见 §4。任何阶段若出口门不达标，**停在该阶段**，不得跳到下一阶段。

---

## 3. 阶段详细任务

> 任务卡片字段：Description / Acceptance（验收）/ Verify（验证命令）/ Dependencies（依赖）/
> Files（改动文件）/ Scope（S/M/L，估计改动规模）。

### 阶段 0：决策冻结 + 文档同步（第 1 周）

#### T0.1 拍板三项关键决策
**Description**：对登记册 §3 的第 6/7/8 项（行业定位 / 场景包是否版本化资产 / 收口与验证资源配比）
拍板；其余 §3 待拍板项（1/2/3/5/9/10）若不影响未来 12 周执行，明确标注"后置，不阻塞本计划"。
**Acceptance**：
- [x] 行业定位已拍板（2026-08-15）：不锁定行业——平台场景无关（ADR-0009），以参考场景包支撑验证
- [x] 单租户优先已由 ADR-0004 拍板，不再重复讨论；本计划按"单组织私有部署"推进
- [x] 登记册 §3 每项标记为"已拍板（记录见 ADR/正文）"或"后置（不阻塞）"
**Verify**：`doc/decisions/README.md` §3 无可阻塞本计划的 open 项。
**Files**：`doc/decisions/README.md`（+ 需要时新增 ADR-0009）。**Scope**：S。

#### T0.2 冻结范围决策
**Description**：把 S3/S4、场景包资产化、前端视觉/主题/排版打磨正式移入冻结清单（§4），登记为已采纳决策。
**Acceptance**：
- [x] §4 冻结清单写入登记册 §2（"已采纳：冻结项"）
- [x] 未来 12 周任务清单不引用被冻结项
**Files**：`doc/decisions/README.md`、本文 §4。**Scope**：S。

#### T0.3 文档纠偏
**Description**：清理过时状态——`product-design.md` §6 M1 验收标准标注过时、README 里程碑 M6 表述统一、
`agent-framework-upgrade.md` 状态栏同步 S2 Phase 2 已落地。
**Acceptance**：
- [x] 文档状态栏（登记册 §5）与代码实况一致
- [x] 无"宣称已完成、实际未做"的误导性表述
**Files**：`doc/product-design.md`、`README.md`、`doc/specs/agent-framework-upgrade.md`。**Scope**：S。

#### T0.4 建立治理缝隙清单
**Description**：把战略 §3 六条缝隙落成带 owner + 目标里程碑 + 现状源码定位的清单。
**Acceptance**：
- [x] 六条缝隙逐条标注 owner + 对应阶段任务编号（T1.1–T1.6）
- [x] 每条附"当前源码位置 + 目标行为"，可直接作为阶段 1 任务输入
**Files**：`doc/gaps.md`（新）。**Scope**：S。

**阶段 0 出口门**：登记册 §3 无可阻塞项；§4 冻结清单已登记；`doc/gaps.md` 就绪。

---

### 阶段 1：生产化收口（第 2–4 周）

> 六条缝隙（战略 §3）+ M5 企业身份。这是"能否进真实生产"的门槛。

#### T1.1 ABAC 预过滤接口（缝隙 #1）——已交付知识域 + 职级，完整属性规则后置
**Description**：已实现 `PolicySubject` + `build_acl_filters()`，统一产出知识域 `source IN (...)`
和文档密级 `level <= role.level` 条件；KB 可见性由 T1.2 在进入检索前限制。
**Acceptance（当前交付范围）**：
- [x] `governance/policy.py` 提供统一过滤构造接口，检索层消费其结果
- [x] 知识域与职级限制在召回前生效，有测试验证
- [ ] 后置：部门与独立敏感度规则、通用 `evaluate(用户, 资源, 动作, 上下文)`；当前 `User` 无部门属性，不宣称跨部门 ABAC 已完成
**Verify**：`make test-unit` + 集成测试（越权召回=0）。
**Dependencies**：T0.4。**Files**：`governance/policy.py`（新）、`governance/capability.py`、`rag/retrieve.py`、`api/dataplane.py`。**Scope**：L。

#### T1.2 `visible_kb_ids` 语义落地（缝隙 #4）
**Description**：`visible_kb_ids` 已作为数据面 KB ACL 生效，runner 在检索前限制可搜索知识库。
**Acceptance**：
- [x] 非 admin 只检索可见 KB；空列表拒绝检索（测试验证）
- [x] 保留字段并明确 ACL 语义；不采用更名/移除方案
**Verify**：`make test-unit` + `make test-integration`。
**Dependencies**：T1.1。**Files**：`governance/capability.py`、`api/dataplane.py`、`agent/runner.py`。**Scope**：M。

#### T1.3 per-session 模型绑定生效（缝隙 #3）
**Description**：`get_chat_model_for_conversation()` 已在 REST/IM 两路按会话解析模型。
优先级为会话绑定 → 实例绑定 → 灰度候选 → 全局激活；无效绑定继续 fallback。
**Acceptance**：
- [x] 会话绑定优先于全局激活（测试验证）
- [x] 未绑定/绑定失效按上述优先级回退
**Verify**：`make test-integration`（多会话模型路由）。
**Dependencies**：None。**Files**：`agent/runner.py`、`llm/resolver.py`、`governance/models.py`。**Scope**：M。

#### T1.4 同步/流式流程收敛（缝隙 #6）
**Description**：`run_agent` 与 `run_agent_stream` 两套高度相似流程易漂移。抽共享的"策略准备 → 执行 → 后处理 →
审计"管线，两路共用（任务维度 event 已由 default_bus 闭合，此处闭合对话维度）。
**Acceptance**：
- [x] 两路经同一 pipeline 入口，审计字段一致（有测试锁定）
- [x] 流式逐 token 语义不破坏（现有企微流式测试绿）
**Verify**：`make ci-local` 全绿 + 新增管线单测。
**Dependencies**：None。**Files**：`agent/runner.py` 内共享准备/收尾函数、`agent/runner.py`、`api/dataplane.py`。**Scope**：L。

#### T1.5 单组织会话键与实例隔离（缝隙 #2）——当前范围已落地，多租户后置
**Description**：按 ADR-0004 单组织部署，`Conversation.tenant_id` 默认为 `default`，不参与运行时会话键。
T3.5 已落实实例维度：无实例入口使用 `{channel}:{user_id}`，指定实例使用
`{channel}:{user_id}:agent:{agent_id}`，历史与摘要按 `conversation_id` 隔离。
**Acceptance（当前交付范围）**：
- [x] 会话表含 `tenant_id` / `agent_id`，IM 专属 Bot 按实例写入并隔离会话
- [x] 同用户跨实例会话分离；全局 Bot 入口 `agent_id=None`，不指向默认员工
- [ ] 后置：多租户身份及会话、审批、审计、MCP、缓存全链路隔离
- [ ] 后置：长期记忆按实例隔离；当前 `UserMemory` 加载以用户为范围，同用户跨实例共享，不能把会话隔离等同于记忆隔离
**Verify**：`tests/unit/test_conversation_tenant.py` + `tests/unit/test_multi_bot_channels.py`（单组织实例会话隔离，不代表多租户验收）。
**Dependencies**：None（DB 迁移，遵循"高风险改动先确认"）。**Files**：`governance/models.py`、`db.py`。**Scope**：L。

#### T1.6 工具结果统一后处理（缝隙 #5）——管道已落地，字段/行级策略后置
**Description**：网关和审批执行共用 `sanitize_tool_result()`：先执行工具可选 redactor，
再转为字符串并将原始内容限制到 4000 字符（超限另附截断提示）。未声明 redactor 的结果只受长度限制。
**Acceptance（当前交付范围）**：
- [x] 网关与审批执行共用后处理，已声明 redactor 的工具先脱敏
- [x] 所有工具结果受长度限制
- [ ] 后置：基于输出 schema 的字段分级、行级过滤及未声明类型默认拒绝；截断不等于敏感字段脱敏
**Verify**：`make test-unit` + MCP 工具集成测试。
**Dependencies**：None。**Files**：`tools/redactor.py`（新）、`tools/base.py`、`tools/gateway.py`。**Scope**：M。

#### T1.7 SSO + 组织同步 + 离职撤权（M5 企业身份）——已按现实收口
**范围决策（2026-08-13）**：
- ✅ **离职即时撤权已闭环**（既有机制）：`User.is_active=False` → `compute_capability_set` 抛 PermissionError（数据面拒绝）+ 登录/act-as/IM `select_by_im`/审批执行/结果推送/任务 Step 全链路拒绝。已有 `test_capability.py` / `test_auth.py` / `test_admin_revocation.py` / `test_gateway.py` / `test_approval.py` / `test_approval_push.py` / `test_step_executor.py` 等全面覆盖，无需新代码。
- ⏸ **OIDC/SAML SSO 登录 + 组织架构同步后置**：属外部环境依赖（需真实 IdP / 组织目录，配置驱动），无真实环境时不可测、纯属推测性实现；待有真实部署环境后再排期。本地账号密码登录保持现状。
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

**阶段 1 当前范围出口门**：单组织部署下已交付的治理链路通过 `make ci-local`，策略模拟器可演示。T1.1/T1.5/T1.6 的后置边界见 `doc/gaps.md`，不计作完整 ABAC、多租户或字段/行级治理已完成。

---

### 阶段 2：评测与发布系统（第 5–8 周）✅ 已完成（2026-08-15）

> 以"治理安全"为核心价值，必须能**自动证明不越权、不回退**。
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
- [x] 控制面看板可见（可作为 T3.4 使用效果看板的底子）
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

### 阶段 3：参考场景与真实使用验证（第 9–12 周，行业无关——见 ADR-0009）

> 证明项目技术价值的关键环节。优先级等同甚至高于阶段 2。
> 按 ADR-0009（2026-08-15 拍板）：平台**场景无关、不锁定行业**，垂直场景由采用者自定义；
> 本阶段交付「参考场景包」+ 真实使用验证。

#### T3.1 参考场景包（行业无关）——已完成代码部分（2026-08-15）
**Description**：按 ADR-0009 定义——不锁定单一行业。交付参考场景包：2 个示例 MCP
连接器（工单 + 库存，`examples/connectors/`）+ 3 类示例角色（一线/主管/访客）+ demo 账号
+ 一键种子 `make seed-reference`（幂等）。
**Acceptance**：
- [x] 任何采用者 `make seed-reference` 后参考场景端到端可跑（demo 账号体验能力差异）
- [x] 连接器经 MCP 网关注册（红线 #5）；高危工具 `destructiveHint` → 审批门自动拦截
**Verify**：`make seed-reference` + `make test-unit`（reference 用例）。**Dependencies**：阶段 1
出口门（已达标）。**Files**：`examples/connectors/`、`scripts/seed_reference.py`、Makefile。**Scope**：M。

#### T3.2 三条端到端平台级参考流程（去行业化，2026-09-02 重定义）——✅ 自动化部分已完成（2026-09-11）
**Description**：**平台场景不做垂直行业应用**（2026-09-02 拍板），演示**治理脊柱**而非行业逻辑。
基于 T3.1 通用记录系统（`demo-ticket`/`demo-inventory`，仅为示例数据结构，采用者可替换）串联三条
平台级流程——均**去行业化命名**，RAG 部分留占位（待用户提供文档后再灌入验证）：
1) **查询-创建闭环**（通用工具链）：`search_parts`/`list_tickets` → `create_ticket`，演示 `allowed_tools`
   白名单按角色过滤（`technician` 可查可建、`guest` 看不到工具）+ 多轮参数补全。
2) **高风险操作 HITL 回推**（审批门）：`reserve_part`/`close_ticket`（`destructiveHint`）→ 审批单
   生成 → 控制面批准（含 once/session/always 三档）→ IM 主动推送结果回发起员工，演示纵深防御
   （网关门 + 连接器侧 `bycall_role` 复核）。
3) **多轮上下文与能力边界感知**（会话隔离 + Runtime Context）：同问不同角色答不同（能力披露）、
   同用户多轮上下文连续、跨用户/跨会话隔离；预留 RAG 引用占位（知识库为空时仅验证 budget/去重管线不报错）。
**Acceptance**：
- [x] 三条流程在 **IM 单聊**与 `api/dataplane` REST 两路端到端走通（含工具调用 + 审批 + 推送）——
  自动化锁定：`tests/integration/test_t32_e2e_rest.py`（12 例）+ `test_t32_e2e_im.py`（6 例，
  mock LLM 脚本 + 真实 demo 连接器内存通道 + fake 渠道推送捕获），`make ci-local` 全绿；
  真机企微人工演示待用户参与（`doc/guides/demo-t3.2.md` 动线就绪；RAG 引用待文档就绪后补，不阻塞）
- [x] 复用 T2.2 权限用例视角对参考场景配置做安全回归（越权召回=0——临时 KB 验证召回前 ACL
  过滤对参考角色 scope/level 生效、工具越权幻觉调用被拦、审批前不执行 + REST 渠道不误推）
**Dependencies**：T3.1。**Files**：`tests/integration/test_t32_e2e_rest.py`、`test_t32_e2e_im.py`、
`scripts/seed_reference.py`（IMBinding 同步）、`tools/gateway.py`（可选参数 schema 修复）、
`doc/guides/demo-t3.2.md`、`doc/specs/t3.2-platform-flows.md`。**Scope**：L。

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

#### T3.5 多 Bot 渠道体系与管理面板重组 —— ✅ 已完成（2026-09-02，ADR-0012）
**Description**：产品方向拍板为多 Bot 平台（用户 2026-09-02）。① 网关多槽位：`ChannelManager`
支持 per-Agent 企微长连接（`Agent.im_config` 从"只写不读"改为真正生效），入站消息按连接路由到
对应实例，会话键落 `:agent:{id}` 后缀（红线 #2 实例维度）；全局默认 Bot（SystemSetting）保留兜底。
② 控制面归一：渠道凭证收敛为"全局默认（渠道接入页）+ per-Agent 专属（数字员工详情）"两个作用域，
bot_id 全局唯一校验，im_config 变更/启停/删除自动重载槽位；`im/status` 返回连接列表。
（2026-09-22 修订：per-Agent 渠道配置泛化为**多渠道**——同一实例可接入多个不同渠道、
同一凭证全局唯一，见 ADR-0012 修订记录与 `src/bycall/gateway/channels.py`。）
③ 管理面板 IA 重组：导航按"配置/运行"重分组，数字员工改列表+详情页分区编辑，
Dashboard KPI 修复，对话测试页补数字员工选择器（真实使用必需，非视觉打磨）。
（后续演进：面板二次重组为数字员工 Hub——渠道接入页与装配资产页并入 `/agents` 子页，
见 ADR-0012 修订记录 2026-09-02。**再后续：该 Hub 已于 T3.6 拆回独立页面，见 ADR-0013。**）
（渠道侧演进（2026-09-23）：预留的渠道抽象已由**飞书渠道**实装验证——自研长连接协议层 +
卡片伪流式，见 [ADR-0015](decisions/0015-feishu-gateway.md)；渠道注册表
`SUPPORTED_CHANNEL_TYPES` 现为 wecom/feishu。）
**Acceptance**：
- [x] 配置了专属 Bot 的实例：企微私聊消息路由到该实例（`Conversation.agent_id` 落库），
  同一用户与两个 Bot 对话产生两条独立会话
- [x] `wecom_bot_id` 冲突（Agent 间 / 与全局默认）被 400 拒绝；幂等键含 Bot 维度不串扰
- [x] 实例禁用/删除后对应连接停止，滞留消息礼貌拒绝（不回落默认实例）
- [x] `make ci-local` 全绿 + `npm run build` 通过
**Dependencies**：无（数据面 per-Agent 能力集/会话键/模型链已就绪）。**Files**：
`gateway/manager.py`、`gateway/base.py`、`gateway/wecom.py`、`agent/im_handler.py`、
`admin/api.py`、`frontend/src/views/`（Channels/AgentDetail/Agents/Layout 等）。**Scope**：L。

#### T3.6 控制台面板重组：取消「默认员工」 + 实例/资产/渠道三分 —— ✅ 已完成（2026-09-22，ADR-0013）
**Description**：用户 2026-09-22 提出的四项返工（默认员工不该存在 / 面板太杂 / 列表不清晰 /
详情翻看麻烦）。① 取消「默认员工」：`init_db` 不再灌 `Agent(name="default")`，`Agent.is_default`
字段与全链路 UI/守卫移除（含删除守卫、Chat 的「默认实例」选项），存量种子实例幂等清理
（仅当完全等于种子足迹且无会话引用）；全局默认 Bot 澄清为**渠道入口**，其入站会话无实例归属。
② IA 三分：数字员工只留实例列表/详情；提示词/模型/Skill/MCP/知识库/文档独立成「能力资产」组；
渠道接入回归独立一级页面；`/agents/*` 旧路径保留重定向。③ 列表瘦身：主表五列 + 展开行明细，
装配列显示生效来源（实例绑定 / 沿用角色 / 全局激活）而非覆盖项原样 dump，KPI 去「默认实例」。
④ 详情左锚点分段（六段，右侧只渲染当前段）+ 脏标记/放弃修改 + 「生效预览」（选用户 → 后端
策略模拟按 `agent_id` 收窄后的真实能力集）。
**Acceptance**：
- [x] 新库零数字员工；存量种子实例按规则清理（动过的 / 有会话引用的一律保留）——
  `tests/unit/test_agent_no_default.py`（5 例，含旧库带 `is_default` 列不报错）
- [x] 策略模拟支持实例维度：实例收窄生效、实例不存在 404、可见性先于能力集判定——
  `tests/unit/test_policy_simulate.py`（+3 例）
- [x] 前端：`npm run build` 通过；旧深链（`/agents/channels` 等）重定向生效；列表/详情/渠道页
  不再出现「默认」字样
- [x] 新建不要求管理员填 slug：内部标识由后端按名称自动生成（`admin.api._auto_agent_slug`，
  中文名退化为 `agent`/`agent-<4 位随机>`，同名加随机后缀），显式传入仍兼容——
  `tests/unit/test_agent_auto_slug.py`（4 例）
- [x] `make ci-local` 全绿
**Dependencies**：无（治理脊柱已支持 `agent_id` 维度能力集）。**Files**：`governance/models.py`、
`db.py`、`admin/api.py`、`admin/schemas.py`、`frontend/src/{router.js,views/*}`、
`tests/unit/{test_agent_no_default,test_policy_simulate}.py`。**Scope**：L。

#### T3.7 控制台导航精简合并（19 项 → 8 项）—— ✅ 已完成（2026-09-23，ADR-0014）
**Description**：用户 2026-09-23 反馈「侧边栏内容太多了」。诊断：ADR-0013 的三分解决了关注点混淆，
但导航仍是 6 组 19 项，1440×900 下导航区实测 1200px 内容 / 792px 可视——必须滚动才能看全。
按用户拍板的「激进」口径合并：① 同源页面并为 **Hub tab 容器**（页面零删除）——组织与权限
（用户/角色/策略模拟）、数字员工（实例列表/渠道接入）、能力资产（提示词/模型/Skill/MCP/发布回滚）、
知识库（知识库/文档）、运行记录（会话/任务）、审计与指标（审计/运营指标）；② 仪表盘改为**首页位**
（不占分组标签）；③ 运行时设置移入**顶栏齿轮**；④ 侧边栏收敛为 4 组 8 项；⑤ 导航模型收敛为单一
真相源 `frontend/src/nav.js`（路由表、侧边栏、面包屑、tab 全读它）；⑥ 旧路径 21 条重定向保留。
**Acceptance**：
- [x] 侧边栏 8 项 / 4 组，1440×900 不再溢出（实测 792px / 792px；旧版对照 1200px / 792px）
- [x] 16 个原一级页全部可达：Hub tab 逐个渲染出自身内容（16/16 抽测通过）
- [x] 旧路径 21 条重定向命中新页面与对应 tab（抽测 18 条全绿，含 3 条 `kb_id` 深链落到文档 tab
  且保留上下文）；`/agents/:id` 详情页不受影响
- [x] tab 与地址栏同步（`?tab=`，非法值归一化写回）；切换 tab 用 `replace`，不占浏览历史
- [x] Hub 内页头不再重复渲染标题（`provide('bcHubMode')` → `PageHeader` 省略 h1）
- [x] `npm run build` 通过
**Verify**：`npm run build` + 浏览器逐 tab 走查（16 tab 内容/面包屑/高亮 + 18 条重定向 + 侧边栏溢出实测）。
**Dependencies**：无（纯前端）。**Files**：`frontend/src/nav.js`（新）、`frontend/src/views/Hub.vue`（新）、
`frontend/src/router.js`、`frontend/src/views/Layout.vue`、`frontend/src/components/PageHeader.vue`、
`frontend/src/views/{Dashboard,Knowledge,KnowledgeBases}.vue`。**Scope**：M。

#### T3.8 控制台可信配置与任务闭环（2026-10-18，已完成）

用户明确要求实施前端优化并启动项目，范围按 [ADR-0017](decisions/0017-console-trust-and-task-flow.md)。对齐 T3.2 真实验证、T3.3 审批及 T3.6/T3.7 现有控制台。

- [x] A：分区提交、深复制基线、放弃修改、离开保护及局部错误。
- [x] B：显式继承、生效预览范围与时效、配置检查、审批摘要/分页、连接和索引发布状态。
- [x] C：首页待办、真实记录关联、筛选上下文、窄屏/焦点/中文输入法。
- [x] 验证：前端构建与回归、make ci-local、启动服务、浏览器视觉与静态复查。

验收记录见 [控制台复查](guides/console-ux-review.md)。本次恢复仅覆盖上述可用性审查，其他冻结项不变。

#### T3.4 使用效果看板（后置，待 T3.2 与真实使用数据）
**Description**：延迟 / 成本 / 人工接管率 / 失败率 / 重复执行率（T2.5 五维）。**当前后置**——待 T3.2 平台流程跑通且有真实使用数据后，再基于 T2.5 五维指标做基线 vs 使用后对比（复用 `observability/metrics`，不另造埋点）。
**Acceptance**：
- [ ] 基线 vs 使用后数据对比（需真实或模拟业务数据）
- [ ] 复用 T2.5 指标
**Dependencies**：T3.2、T2.5。**Files**：前端看板。**Scope**：M。

**阶段 3 出口门**：拿到真实使用数据——**未授权召回=0、引用覆盖率、工具参数正确率、任务成功率、人工接管率**。
数据不达预期则收缩范围或调整方向（重新评估也是计划的一部分，不是失败）。

---

## 4. 冻结清单（问题 #4）

| 项目 | 状态 | 恢复条件 |
|---|---|---|
| S3 skill 演化 / S4 分发 | 冻结 | 阶段 1+2 完成，真实使用证明记忆/执行链路稳定后 |
| 场景包版本化/导入导出 | 冻结 | 场景包在真实使用中证明价值后 |
| 前端视觉/主题/排版打磨 | 冻结（T3.6 / T3.7 为用户明确提出的信息架构返工，按恢复条件已豁免，见 ADR-0013 §决策 6、ADR-0014 §决策 9） | 真实使用或审计明确提出需求时 |
| 成本预算 / 推送 outbox 完善 | 冻结 | 多用户真实流量出现后再排 |
| 多 Agent 群聊、低代码画布、Skill 公共市场 | 不做 | 战略 §7 已明确，维持 |
| QM 群聊共同授权（audience floor） | 冻结 | 有群聊场景的真实需求时 |

## 5. 风险与依赖

| 风险 | 影响 | 缓解 |
|------|------|------|
| 单人 12 周排满易延期 | 高 | 阶段 1/2/3 只保留 P0 任务；每两周砍范围评审 |
| 治理价值未经真实使用验证 | 高 | 阶段 3 数据不达预期即收缩范围或调整方向，明确写进出口门 |
| 六条缝隙牵一发动全身 | 中 | 按 T1.1→T1.2→…→T1.6 顺序，先做有测试锚点的项 |
| DB 迁移/多租户改动风险 | 中 | 遵循"高风险改动先停下确认"，迁移前备份 |

## 6. 与现有文档的关系

- 本文是**执行主方向**，取代默认的"继续 M7+ / S3-S4 堆能力"。
- 里程碑历史沿革见 `product-design.md` §6；架构红线（`architecture.md`）不变，仍是硬约束。
- 任务执行时仍遵守 `AGENTS.md`（codegraph 先探索、worktree、中文 commit、CHANGELOG 强制）。
- 本文的登记状态见 `doc/decisions/README.md` §2 / §5；六条缝隙明细见 `doc/gaps.md`（阶段 0 产出）。
## RAG 缺陷修复批次（2026-10-13）

属于 T1.1 / T1.9 / T2.1 / T3.2 的既有链路修复，见 [ADR-0016](decisions/0016-rag-publication-and-evidence.md)。

- [x] 批次 1：generation 发布门、ACL 与失败索引一致性、统计。
- [x] 批次 2：child 召回、父块精确读取、ACL 与来源保留、严格 filter。
- [x] 批次 3：统一候选重排、多轮改写、检索状态与实际证据引用、回归评测。

批次 1 验证：943 个测试、四类评测与权限安全评测通过（`make ci-local`）。

批次 2 验证：951 个测试、四类评测与权限安全评测通过（`make ci-local`）。

批次 3 验证（2026-10-15）：966 个测试、35 条四类综合评测、21 条权限安全评测
全部通过（`make ci-local`）。当前运行行为与验收边界见 [RAG 指南](guides/rag.md)；
真实模型召回质量、无答案阈值与业务语料验收仍需后续验证。
