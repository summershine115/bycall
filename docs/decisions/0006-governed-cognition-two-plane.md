# ADR-0006：受治理认知——两平面信任模型

- **状态**：已采纳
- **日期**：2026-08-06
- **背景**：从"agent 不可学习"到"受治理的、会成长的团队协作者"
- **关联**：`product-design.md` §0/§2.4/§6/§8、`architecture.md` 红线 #4/#7、ADR-0005、
  `docs/tencent-agent-memory-benchmark.md`、`docs/qm-benchmark.md`

## 背景

Bycall 的产品定位是"企业级**数字员工**"——一个会成长、能继承团队经验的协作者。但当前的记忆设计
与这个定位冲突：

- **既有立场**（`governance/models.py` `UserMemory` docstring）："Bycall 的记忆不由 LLM 自主编辑
  （企业场景：记忆是组织管控的，不是 LLM 自己写的）。M3 先做表结构 + admin 手动管理，LLM 自动提取
  记忆留后续迭代。"
- **落地现状**：`agent/memory/` 是空占位；红线 #4"分层记忆、agent memory ≠ RAG"只是口号，未落地。
  实际记忆 = admin 手填 `UserMemory`（`load_user_memory` 返回**全部**注入 system message）+ 压缩
  `summary` + 历史窗口。
- **根因诊断**：Bycall 把**策略平面**的不信任（正确且必要的）蔓延到了**认知平面**——用治理授权决策
  的直觉，去管了一个本质是"agent 怎么变聪明"的东西。结果是把 agent 变成"带权限的、永不学习的
  问答机"。对一个团队协作者而言，"无法沉淀共享经验"是比"偶尔泄露一条记忆"更大的产品缺陷：前者
  让它永远停在第一天，后者是可治理、可审计、可回滚的工程问题。

调研 TencentDB Agent Memory（见 `tencent-agent-memory-benchmark.md`）后确认：TAM 用 L0-L3 分层 +
资产可见性 + 有界召回，解决了"agent 如何继承经验"，但它的信任模型（LLM 自主写记忆进 system 权重）
比 Bycall 宽松，直接照搬违反红线 #7。

## 决策

采用**两平面信任模型**：策略平面保持现状严苛不变，认知平面开放但受治理。

### 平面 1：策略平面（授权决策）—— 不变

agent **永远碰不到**，保持现状的全部严苛：

- 能力集 / 工具白名单（无权工具 LLM 看不到）
- RAG ACL 预过滤（红线 #1）
- 破坏性操作 HITL 审批（`ApprovalRequest`）
- 运行时能力主动披露且不可提权（红线 #9）

这一平面与 QM 同构（qm-benchmark §1），是 Bycall 的护城河，**本决策不放松一分**。

### 平面 2：认知平面（学习与记忆）—— 开放 + 发布门

agent **可自由生成**认知产物（记忆提取、Skill 蒸馏、模式学习），但受四条硬约束：

1. **默认 private**：新生成的记忆/Skill 默认只属于发起者，不自动共享。
2. **发布门**：要成为团队/组织级共享资产，必须过一道 admin 审核门（`pending` 草稿 → 审核通过 →
   `promote`）。复用 Bycall 已有的 `ApprovalRequest` / HITL 模式。
3. **降权注入**：认知产物（无论草稿还是已发布）召回时**降权进 user 角色**，不进 system（红线 #7
   不变）。这与 TAM"记忆享 system 权重"相反——是 Bycall 企业场景的正确选择。
4. **永不改写策略**：认知产物再丰富，也授予不了任何它本来没有的权限——授权判定在策略平面，
   记忆只是上下文数据。

> 落地路径（M3 记忆层主体，借鉴 TAM L0-L3）：L0=`Message`、L1=`UserMemory`(atom)、L2=场景块、
> L3=稳定画像；混合召回（BM25 + 向量 + RRF）+ 三重预算上限（条数 / 字符 / 超时）。详见
> `tencent-agent-memory-benchmark.md` §2.1–§2.3。

## 与既有立场 / 红线的关系

| 条目 | 关系 |
|---|---|
| 红线 #1（策略不在提示词层） | **强化**：策略平面照旧在代码层，认知产物不进策略判定 |
| 红线 #4（分层记忆、agent memory ≠ RAG） | **落实**：给出落地路径与治理边界，不再是空口号 |
| 红线 #7（不可信上下文不得提权） | **强化**：认知产物明确降权进 user 角色，发布门是其治理化形态 |
| `UserMemory` 旧 docstring 立场 | **逆转**："记忆不由 LLM 自主编辑" → "agent 生成 + 组织管控发布；记忆永不越权" |

> 关键澄清：本决策**不削弱安全**，而是把"严"放在正确的平面。开放认知 + 发布门让信任边界比
> "全面禁止"更清晰、更可审计——草稿有来源、发布有审批、召回有预算、注入有降权，全程留痕。

## 备选方案

### A. 全自主（TAM 原样）

agent 自由生成记忆 + 自由进 system 权重。

- **否决**：违反红线 #7（不可信内容不得进 system）；企业场景下 agent 基于"自己编的记忆"行动有
  合规责任；TAM 的"自主性"正是 Bycall 不该要的部分。

### B. 维持全锁（现状）

agent 不学习，记忆全靠 admin 手填。

- **否决**：不符合"会成长的团队协作者"定位（§0）；admin 手写每条记忆不可扩展；agent 永远停在
  第一天，丧失区别于"带 RBAC 的搜索框"的全部价值。

### C. 两平面 + 发布门（本决策）

兼顾安全与成长，信任边界显式可审计。**采纳。**

## 后果

**优点**：

- agent 可学习、可复利——团队走过的路成为下一个 agent 的起点。
- admin 负担可控：只审"发布"，不审"生成"；草稿可批量审或拒绝。
- 安全边界更清晰：策略/认知两平面分离，比"一刀切禁止"更可审计、可回滚。
- 闭合红线 #4 的空档，给 M3 记忆层明确的落地蓝图。

**代价**：

- 需新建：发布门（草稿态 + 审核流）、可见性模型（private/team/restricted）、召回预算。
- L0-L3 提取 Pipeline 是真实工程量（触发式生成 + 混合召回 + 去重 + 清理）。
- 过渡期须保证现有同步治理链路（`run_agent`）零回归——记忆层是增量叠加，不替换会话状态层。

## 何时重新评估（任一满足）

- **客户场景转向强监管高可靠执行单元**（金融交易/医疗决策/政务审批）：agent 基于 self-generated
  memory 行动产生合规责任 → 重新收紧认知平面（甚至回退到全锁/只读/确定性回放）。
- **发布门成为瓶颈**：admin 审核跟不上生成速度 → 考虑分级自动晋升（低风险偏好/事实类自动 promote，
  高风险/约束类必审）。
- **出现跨租户记忆泄漏**：发布门或可见性模型被绕过 → 暂停认知平面，重审隔离边界。

## 相关

- 记忆层落地：`src/bycall/agent/memory/`（待建）、`src/bycall/agent/session_store.py`（会话状态层，
  L0/summary 基座）、`src/bycall/governance/models.py`（`UserMemory`/`ApprovalRequest`）
- 发布门复用：`src/bycall/tools/approval.py`（HITL 模式）
- 治理链路：`src/bycall/agent/runner.py`（`run_agent`）、`src/bycall/agent/runtime_context.py`
- 基准：`docs/tencent-agent-memory-benchmark.md`（TAM）、`docs/qm-benchmark.md`（QM 同构治理）
