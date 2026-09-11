# T3.2 平台级参考流程手工演示脚本（IM 单聊 + REST）

> 去行业化（2026-09-02）：平台不做垂直行业，演示治理脊柱而非业务逻辑。
> 前置：`make seed-reference && make dev` 已启动，MCP 连接器已挂载。
> 账号：`demo-guest` / `demo-tech` / `demo-lead`（企微 demo 绑定 `DEMO_*_001`）。
> 自动化锁定：REST 路见 `tests/integration/test_t32_e2e_rest.py`（12 例），
> IM 单聊路见 `tests/integration/test_t32_e2e_im.py`（6 例，fake 渠道）——CI 全绿
> 即三条流程的治理语义已回归保护；本脚本是**真机企微**的人工演示动线。

## Flow A：查询-创建闭环（通用工具链）

**目的**：证明 `allowed_tools` 白名单按角色过滤（LLM 看不到 = 不可调）+ 多轮参数补全。

1. `demo-guest` 问："你能帮我查一下 T-1001 吗？"
   - 期望：文本拒答/引导（"需要相应权限"），`tool_calls` 为空，审计 `decision=answered` 但无工具调用。
2. `demo-tech` 问："帮我查一下工单 T-1001"
   - 期望：调用 `mcp__demo-ticket__query_ticket` 返回 `T-1001` 详情。
3. `demo-tech` 说："按关键词搜一下 bearing"
   - 期望：调用 `mcp__demo-inventory__search_parts` 返回 `BRG-6205` 等。
4. `demo-tech` 说："帮我建一条标题为'测试-平台流程A'的记录，优先级 high"
   - 期望：调用 `mcp__demo-ticket__create_ticket` 返回新 `ticket_id`（如 `T-1003`）。
5. `demo-lead` 重复 2-4
   - 期望：同样成功，且在回答中可提及"可执行高危操作"（因多一个 Skill `platform-risk-note`）。

REST 复现示例（登录 admin 后 dev act-as 扮演 demo 账号，cookie 全程保持）：

```bash
BASE=http://localhost:8000
# 1. 登录拿会话 cookie
curl -s -c /tmp/bycall.jar -X POST $BASE/api/login \
  -d 'name=admin&password=admin123' >/dev/null
# 2. 扮演 demo-tech（dev only；先查用户 id）
TECH_ID=$(curl -s -b /tmp/bycall.jar $BASE/api/admin/users | jq -r '.[] | select(.name=="demo-tech") | .id')
curl -s -b /tmp/bycall.jar -c /tmp/bycall.jar -X POST \
  "$BASE/api/admin/act-as?target_user_id=$TECH_ID" >/dev/null
# 3. 以 demo-tech 身份对话（响应含 tool_calls / decision / context_trace）
curl -s -b /tmp/bycall.jar -X POST $BASE/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"帮我查一下工单 T-1001"}' | jq
# 4. 演示完清除扮演态
curl -s -b /tmp/bycall.jar -X POST $BASE/api/admin/clear-act-as >/dev/null
```

## Flow B：高风险操作 HITL 回推

**目的**：证明 `destructiveHint` → 审批单 → 三档授权 → IM 推送。

1. `demo-tech` 尝试："帮我关闭 T-1001，原因是测试"
   - 期望：**不直接执行**。若 LLM 尝试调 `close_ticket`，网关会因 `allowed_tools` 无该工具而看不到；若强行（测试用直调），连接器侧 `bycall_role=technician` 二次拒绝。演示重点改由 `demo-lead` 发起。
2. `demo-lead` 说："关闭工单 T-1001，原因是测试关闭"
   - 期望：LLM 产生 `close_ticket` 调用被审批门拦截，回复"已提交审批"，控制面 `GET /api/admin/approvals?status=pending` 可见一条 `tool_name=close_ticket`。
3. 控制面批准：`POST /api/admin/approvals/{id}/approve {"note":"同意","grant_scope":"once"}`
   - 期望：`status=executed`，`result_text` 含 `closed`，发起人 IM 收到推送"你的关闭工单已执行"。
4. 同工具再次让 `demo-lead` 关 `T-1001`（已关闭会报已关闭，换 `reserve_part` 演示三档）：
   - `reserve_part` 演示：`demo-lead` 说"预留 BRG-6205 2件 关联 T-1003" → 审批 → 批准选 `session` → 同会话再预留同物料免审，换新 `conversation_id` 需重新审批；`always` 则跨会话免审。
5. 推送验证：企微单聊窗口应收到 3 的结果卡片；`auditlog` 有 `grant_id` + `reviewed_by=grant:*`。

## Flow C：多轮上下文与能力边界感知

**目的**：证明会话隔离 + Runtime Context + 空 RAG 不崩。

1. `demo-tech` 单聊多轮：
   - 问："我是谁？我能用什么工具？" → 期望：据 `capability` 正确列出 `query/list/create/search`，不编造高危工具。
   - 紧接说："刚才建的那条记录标题是什么？" → 期望：引用会话历史中的 `T-1003` 标题（多轮连续）。
2. 同问不同角色："你能关闭工单吗？"
   - `demo-tech` → "需要主管审批/无权限"
   - `demo-lead` → "可以，但需审批"（回答不同，证明能力按角色分发）
3. 新会话隔离：`demo-tech` 新开 `conversation_id` 问："上一会话的记录标题是什么？"
   - 期望：答"不知道/无上下文"，不串台。
4. 空 RAG 验证：问"超出知识库范围的私有数据" → 期望：`retrieved=0` 仍正常回答"需要相应权限"，`context_trace` 有 `context_report`（budget/去重/过滤统计），不报错。

## 真机企微测试要点

- 企微侧用 `DEMO_TECH_001` / `DEMO_LEAD_001` / `DEMO_GUEST_001` 分别绑定 `demo-*` 账号（或在控制台改 `im_bindings` 为真实企微 userID）。
- 群聊仍拒绝（红线 #2 未解除），仅测单聊。
- RAG 引用待用户提供文档后再测，当前空库为预期状态，无需验证引用覆盖率。
