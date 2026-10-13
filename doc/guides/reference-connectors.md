# 参考场景包（Reference Scenario Pack）

> ADR-0009：Bycall 是**场景无关**的开源安全可控 agent 平台——平台核心不做行业押注，
> 垂直场景（制造业运维 / ITSM / ……）由采用者自定义。`examples/connectors/` 是「参考场景包」：
> 一组可独立运行、可复制改造的示例 MCP 连接器，用于演示接入方式与治理语义，
> **不是平台的一部分，也不是产品方向**。

## 包含什么

| 连接器 | server 名 | 工具（注册后全名 `mcp__<server>__<tool>`） | 演示重点 |
|---|---|---|---|
| `ticket_server.py` | `demo-ticket` | `create_ticket` / `query_ticket` / `list_tickets` / `close_ticket`* | 工单读写；`close_ticket` 高危 → 审批门 + 连接器侧角色复核（纵深防御） |
| `inventory_server.py` | `demo-inventory` | `search_parts` / `get_part_stock` / `reserve_part`* | 物料查询；`reserve_part` 高危 → 审批门 |

\* 标注 `destructiveHint=True` 的工具会被 Bycall 审批门自动拦截转 HITL 人审（M4 Slice C），
LLM 无法直接执行——「高危 = 需人审」是平台统一语义，连接器只需声明。

## 快速跑通

```bash
make install          # 依赖（含 mcp SDK）
make seed-reference   # 灌入：3 个示例角色 + 3 个 demo 账号 + 注册 2 个连接器（幂等）
make dev              # 启动 API（MCPManager 启动时拉起连接器子进程并发现工具）
```

种子内容（`scripts/seed_reference.py`）：

| 角色 | 能力（allowed_tools） | 用意 |
|---|---|---|
| `technician` 一线工程师 | 工单读/建 + 备件查询 | 日常执行者：能干活，动不了高危操作 |
| `supervisor` 值班主管 | technician 全部 + `close_ticket` + `reserve_part` | 高危操作与审批的归属角色 |
| `guest` 访客 | 无任何工具 | 治理对比：LLM 根本看不到工具（不是"看到不让调"） |

demo 账号：`demo-tech` / `demo-lead` / `demo-guest`（各带企微 demo IM 绑定，可直接在 IM 模拟器/数据面 API 里体验差异）。

## 改造成自己的连接器

1. 复制任一文件，改 `FastMCP("你的-server名")` —— server 名即工具前缀，也是 `MCPServer.name`。
2. 把内存字典换成你的业务系统调用（REST/SDK/DB）。连接器**不依赖 Bycall**，随便用什么栈。
3. 风险分级：读工具不标注；高危写工具加 `@server.tool(annotations=ToolAnnotations(destructiveHint=True))`。
4. 需要按调用者鉴权/审计时，从 `ctx.request_context.meta` 读 `bycall_user_name` / `bycall_role`
   （Bycall 数据面自动注入，LLM 伪造不了——见 `src/bycall/mcp/context.py`）。
5. 注册：控制台「能力资产 → MCP 工具」页，或直接改 `scripts/seed_reference.py` 的种子表。stdio 填
   `command=<python> args=[<脚本绝对路径>]`；远程服务填 http transport + 凭证（Fernet 加密存储）。
6. 把 `mcp__<server>__<tool>` 加进对应角色的 `allowed_tools` 白名单——工具对 LLM 的可见性
   完全由这白名单决定。

## 设计边界（为什么这么写）

- **平台零行业词汇**：连接器是示例数据（物料刻意工业 + IT 混排），平台核心
  （`src/bycall/`）不 import 本目录——删掉 examples/ 平台照常运行。
- **权限不进提示词**：谁能用什么工具由治理脊柱（角色白名单）在 LLM 之前决定；
  连接器侧角色复核只是纵深防御，不是第一道门。
