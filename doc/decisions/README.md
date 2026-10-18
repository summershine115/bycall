# Bycall 决策登记册（Decision Registry）

> **本文件是项目所有架构/产品决策的唯一入口。** 查找决策先看这里；写新决策遵守 §4 流程。
> 状态约定：✅ 已采纳（含已落地） · 🕐 提议中（proposed） · ⚠️ 待拍板（open） · 🚫 已否决 · ♻️ 已替代 · 🧊 已冻结 · ⏸ 后置

## 1. ADR 索引（正式决策记录）

| 编号 | 决策 | 状态 | 日期 | 文件 |
|------|------|------|------|------|
| ADR-0001 | 抽象 VectorStore 接口（可切换后端） | ✅ 已采纳 | 2026-07-08 | `0001-vectorstore-abstraction.md` |
| ADR-0002 | 渐进式引入 LangGraph（编排+工具先行，Checkpointer 留后续） | ✅ 已采纳 | 2026-07-15 | `0002-langgraph-incremental.md` |
| ADR-0003 | 抽象文档解析引擎（可切换） | ✅ 已采纳 | 2026-07-27 | `0003-parse-engine-abstraction.md` |
| ADR-0004 | 单组织私有部署优先（tenant 预留不强制落地） | ✅ 已采纳 | 2026-07-30 | `0004-single-tenant-priority.md` |
| ADR-0005 | 抽象统一 ABAC Policy 接口（自研，暂不引 OPA/Cedar） | ✅ 已采纳 | 2026-07-30 | `0005-abac-policy-abstraction.md` |
| ADR-0006 | 持久化任务系统（SQLModel 增量，不引独立工作流引擎） | ✅ 已采纳 | 2026-07-30 | `0006-task-system-incremental.md` |
| ADR-0007 | 受治理认知——两平面信任模型（策略平面严苛 / 认知平面开放+发布门） | ✅ 已采纳 | 2026-08-06 | `0007-governed-cognition-two-plane.md` |
| ADR-0008 | 持久化任务执行核心 S1a（`run_tool_loop` 作 Step 内核 + typed event 统一） | ✅ 已采纳 | 2026-08-06 | `0008-agent-task-execution.md` |
| ADR-0009 | 平台场景无关——不锁定行业，以参考场景包支撑真实使用验证 | ✅ 已采纳 | 2026-08-15 | `0009-scenario-agnostic-platform.md` |
| ADR-0010 | 评测与发布门——离线四类评测集 + 灰度发布/一键回滚 | ✅ 已采纳 | 2026-08-15 | `0010-eval-release-gate.md` |
| ADR-0011 | 审批授权三档（once/session/always）持久化与撤销 | ✅ 已采纳 | 2026-08-21 | `0011-approval-grant-tiers.md` |
| ADR-0012 | 多 Bot 渠道体系——per-Agent IM 凭证生效与按连接路由 | ✅ 已采纳 | 2026-09-02 | `0012-multi-bot-channels.md` |
| ADR-0013 | 控制台面板重组——取消「默认员工」 + 实例/资产/渠道三分 | ✅ 已采纳 | 2026-09-22 | `0013-agent-console-ia-default-removal.md` |
| ADR-0014 | 控制台导航精简合并——19 项入口 → 8 项（Hub tab 容器 + 顶栏设置入口） | ✅ 已采纳 | 2026-09-23 | `0014-console-nav-consolidation.md` |
| ADR-0015 | 飞书渠道接入——自研长连接协议层（对齐官方 lark-oapi 1.7.3）+ 卡片伪流式 | ✅ 已采纳 | 2026-09-23 | `0015-feishu-gateway.md` |

| ADR-0016 | RAG 索引发布、父子块与证据链修复 | ✅ 已采纳（批次 1–3 已落地） | 2026-10-13 | [0016](0016-rag-publication-and-evidence.md) |

| ADR-0017 | 控制台可信配置与任务闭环 | ✅ 已采纳（用户要求实施） | 2026-10-18 | [0017](0017-console-trust-and-task-flow.md) |

## 2. 正文中的已采纳决策（未单独成 ADR，位置即权威）

这些决策已拍板并记录在正文文档中，保持原位不迁移，此处仅登记状态：

| 决策 | 状态 | 位置 |
|------|------|------|
| QM 工程化借鉴五件套：消息幂等去重 / 会话租约 / 压缩异步化已落地（2026-08-12，merge 56605ba）；operator error log / 成本预算 / 推送 outbox 未落地 | ✅ 部分落地 | `product-design.md` §6.1、`qm-benchmark.md` §6 |
| 群聊共同授权（audience floor：交集放行/并集拒绝） | 📋 已规划（M3 多用户会话） | `qm-benchmark.md` §2.1、`product-design.md` §2.4 |
| HITL 审批授权三档（once/session/always） | ✅ 已落地 | ADR-0011、execution-plan T3.3 |
| 安全姿态分级（strict/auto/dangerous，org 为地板） | 📋 已规划（P1） | `qm-benchmark.md` §2.2、§4 |
| M4 设计红线：grant 变更/身份冒充/命令审批不给 agent 入口 | ✅ 已采纳（设计约束） | `product-design.md` §8 |
| 运行时能力主动披露且不可提权（Runtime Context） | ✅ 已落地（红线 #9） | `architecture.md` 红线 #9 |
| 失败时保持授权状态（向量侧先行，失败不提交元数据/ACL 变更） | ✅ 已落地（红线 #6） | `architecture.md` 红线 #6 |
| SKILL 信任定位：admin 管控的可信配置，合法注入 system（软隔离） | ✅ 已落地 | `product-design.md` §2.4、红线 #7 |
| 下一阶段主方向：先收口（六条治理缝隙 + M5）→ 评测/发布门 → 参考场景与真实使用验证；冻结 S3/S4 与新能力 | ✅ 已采纳（执行主方向） | `doc/execution-plan.md` |
| 身份映射模式：邀请制（非自注册+待审批） | ✅ 已采纳（沿用现实现） | design §7 |
| 未受邀员工接入：拒绝 + 引导联系管理员 | ✅ 已采纳（沿用现实现） | design §7 |
| 权限缓存：进程内 `(user_id, agent_id)` 缓存，治理变更失效 | ✅ 已落地 | `src/bycall/governance/capability.py` |
| Admin 认证：账号密码已落地；SSO/组织同步依赖真实环境 | ⏸ SSO/组织同步后置 | execution-plan T1.7 |
| 模型优先级：会话绑定 → 实例绑定 → 灰度候选 → 全局激活 | ✅ 已落地 | execution-plan T1.3、`src/bycall/llm/resolver.py` |
| 行业定位 | ✅ 已拍板（2026-08-15）：**不锁定行业**——平台场景无关（开源），垂直场景由采用者自定义，仓库交付参考场景包 | ADR-0009 |
| M5 收口 vs 阶段 3 验证顺序：先收口后验证（严格 0→1→2→3） | ✅ 已拍板（2026-08-13） | execution-plan 阶段总览 |
| 阶段 2 评测与发布系统：四类离线评测集进 CI、快照回放 diff、发布门 + 稳定哈希灰度 + is_active 回滚、五维 Prometheus 指标 | ✅ 已落地（2026-08-15；2026-09-23 修订：控制面补齐发布入口 + 回滚 fail-closed） | ADR-0010、execution-plan 阶段 2 |
| 阶段 3 T3.3 审批授权三档：ApprovalGrant 持久化 + 审批门短路 + 台账/撤销 + 免审执行指标 | ✅ 已落地（2026-08-21） | ADR-0011、execution-plan T3.3 |
| 阶段 3 T3.6 面板重组：取消「默认员工」（无预置实例 + 存量种子清理）、实例/能力资产/渠道接入三分、列表瘦身+展开行、详情左锚点分段 + 生效预览（策略模拟支持 `agent_id`） | ✅ 已落地（2026-09-22） | ADR-0013、execution-plan T3.6 |
| 阶段 3 T3.7 导航精简合并：同源页面并为 Hub tab 容器（导航 19 项 → 8 项 / 4 组，页面零删除）、运行时设置移顶栏齿轮、21 条旧路径重定向 | ✅ 已落地（2026-09-23） | ADR-0014、execution-plan T3.7 |
| 飞书渠道接入：自研长连接协议层（不对齐官方 SDK 的全局 loop 模型，wire format 用官方 golden bytes 锚定）、union_id 优先绑定、p2p 清空 chat_id、卡片伪流式回复、全局默认 + per-Agent 专属双作用域 | ✅ 已落地（2026-09-23） | ADR-0015、`src/bycall/gateway/feishu*.py` |
| 冻结清单：S3/S4（含 Skill 晋升闸门、自动提议）、场景包版本化/导入导出、前端视觉/主题打磨、成本预算/推送 outbox、群聊共同授权；恢复条件见 execution-plan §4 | 🧊 已冻结 | execution-plan §4 |

## 3. 开放决策清单（待拍板）

合并自 `product-design.md` §7、`product-strategy.md` §8、`specs/agent-framework-upgrade.md` 假设。
**拍板后移入 §1 或 §2**，不留悬念。

> **本清单已由阶段 0（2026-08-13）清空**：原 10 项待拍板全部处置完毕（已采纳 / 已排期 / 已冻结 / 后置），
> 处置结果见 §2 新增行与 `doc/execution-plan.md`。新的待拍板项再在此新增（§4 流程不变）。

**已由 ADR 拍板、从开放清单移出的**（历史决策点 → 决策记录）：

| 原决策点 | 结论 | 记录 |
|---------|------|------|
| 单组织私有部署 vs SaaS 多租户 | 单组织私有部署优先，tenant 预留不强制落地 | ADR-0004 |
| ABAC 自研 vs 引入 OPA/Cedar | 自研统一 Policy 接口 | ADR-0005 |
| 任务系统 SQLModel 增量 vs 独立工作流引擎 | SQLModel 增量 | ADR-0006 |
| 记忆归谁：LLM 自主 vs 组织管控 | 两平面：策略平面不变，认知平面开放+发布门 | ADR-0007 |
| 执行层承载：落在 Task/Step/Run；不引入 PI TS 栈；L3 记忆降权进 user | 按 spec 假设 3/4/5 实现 | ADR-0008、ADR-0007 |

## 4. 新决策流程约定

1. **新决策先写 ADR**：`doc/decisions/NNNN-<slug>.md`，编号递增不复用；状态先标 🕐 提议中。
2. **拍板后更新状态**：评审通过或代码落地后标 ✅ 已采纳，并登记到 §1 索引。
3. **正文只留指针**：产品/架构文档写「见 ADR-NNNN」，不在正文里写大段决策论证。
4. **被替代/否决**：标 ♻️ 已替代（指向替代者）或 🚫 已否决，不删除——决策历史也是资产。
5. **待定项集中**：所有待拍板事项进 §3，拍板即移出，杜绝「散落三处各说各话」。

### ADR 模板

```markdown
# ADR-NNNN：<一句话标题>

- **状态**：🕐 提议中 / ✅ 已采纳
- **日期**：YYYY-MM-DD
- **背景**：为什么需要这个决策（问题，不是方案）
- **关联**：相关 ADR / 文档章节

## 背景
## 决策
## 备选方案
## 后果
```

## 5. 正文文档状态一览（统一状态栏）

| 文档 | 状态 | 说明 |
|------|------|------|
| `doc/product-design.md` | 演进中 | 产品形态设计（≈PRD）。核心章节已随实现演进；§5/§6 为历史设计与路线图；当前范围看 execution-plan，§7 指向登记册 |
| `doc/product-strategy.md` | 建议稿（待评审） | 技术策略判断（2026-09-11 去商业化修订，场景无关）。§8 决策点已全部处置：2/3/4 转 ADR-0004/0005/0006，其余处置记录见 §2 |
| `doc/architecture.md` | 随实现演进 | 分层架构 + 9 条红线（当前约束，不可违背） |
| `doc/qm-benchmark.md` | 调研记录（已采纳） | QM 对标；借鉴项映射见 product-design §6.1 |
| `doc/tencent-agent-memory-benchmark.md` | 调研记录（已采纳） | TAM 对标；已升华为 ADR-0007 |
| `doc/specs/agent-framework-upgrade.md` | 已采纳 | S1 执行层（a+b）已落地；S2 记忆层 Phase 1+2 已落地（含 agent 写路径）；S3–S4 已冻结（见 `doc/execution-plan.md` §4） |
| `doc/archive/tasks/plan.md` / `doc/archive/tasks/todo.md` | 历史归档 | S1（a+b）已实现；原任务勾选保留历史，当前进度看 execution-plan |
| `CHANGELOG.md` | 版本记录 | 用户可见变更流水账，不承载「为什么」 |
| `doc/execution-plan.md` | **主方向（阶段 3 进行中）** | 阶段 0 已完成，阶段 1 按单组织范围收口（后置边界见 gaps.md，含 T1.9 上下文预算），阶段 2（评测与发布系统 T2.1–T2.5）已完成；当前处于阶段 3 参考场景与真实使用验证（行业无关，ADR-0009；T3.1/T3.3/T3.5/T3.6/T3.7 已落地） |
| `doc/gaps.md` | 当前交付与后置边界清单 | T1.1/T1.5/T1.6 保留完整 ABAC、多租户/实例记忆隔离、字段/行级治理的后置项；T1.9 上下文预算已落地 |
| `doc/decisions/0010-eval-release-gate.md` | ✅ 已采纳 | 阶段 2 评测与发布系统 ADR（离线用例 / 回放 diff / 发布门 / 灰度回滚 / 五维指标） |
| `doc/decisions/0011-approval-grant-tiers.md` | ✅ 已采纳 | T3.3 审批授权三档 ADR（once/session/always / 审批门短路 / 台账撤销 / 治理边界） |
| `doc/decisions/0012-multi-bot-channels.md` | ✅ 已采纳 | T3.5 多 Bot 渠道 ADR（per-Agent IM 凭证生效 / 按连接路由；全局默认 Bot 为**渠道入口**而非员工，其入站会话无实例归属——见 ADR-0013） |
| `doc/decisions/0013-agent-console-ia-default-removal.md` | ✅ 已采纳 | T3.6 面板重组 ADR（取消默认员工 / 实例·资产·渠道三分 / 列表瘦身 / 详情分段 + 生效预览） |
| `doc/decisions/0014-console-nav-consolidation.md` | ✅ 已采纳 | T3.7 导航精简合并 ADR（同源页面并 Hub tab / 19 项 → 8 项 / 顶栏设置入口 / 21 条旧路径重定向） |
| `doc/decisions/0015-feishu-gateway.md` | ✅ 已采纳 | 飞书渠道 ADR（自研长连接协议层对齐官方 lark-oapi 1.7.3 / union_id 优先绑定 / p2p chat_id 适配 / 卡片伪流式 / 全局+专属双作用域） |
| `doc/guides/evaluation.md` | 随阶段 2 落地 | 评测 CLI 使用说明（make eval / eval-security / --snapshot / --compare） |
| `doc/README.md` | 文档入口 | 阅读顺序、目录职责与维护约定 |
| `doc/open-source-positioning.md` / `doc/open-source-checklist.md` | 建议稿 / 待执行 | 开源定位尚未采纳，清单不是发布通过证明 |
| `doc/archive/` | 历史归档 | 实施笔记与前端接入包，不承载当前待办 |
