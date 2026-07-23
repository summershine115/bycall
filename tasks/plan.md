# M4 实施计划：MCP 工具网关

> 状态：Slice A ✅ · Slice B ✅ · Slice C ✅ · 2026-07-22 · M4 完成

## 1. 目标与范围

**一句话**：让 admin 注册外部 MCP server，其工具被自动发现并纳入治理脊柱，LLM 只能通过「能力集白名单」按角色调用——agent 永不直连 MCP server（红线 #5）。

M4 路线图三块，本计划拆为三个垂直切片，**先做 Slice A（地基）**：

| 切片 | 内容 | 价值 |
|------|------|------|
| **A. 网关 + 注册表**（本次） | MCPServer 实体 + admin CRUD + MCPClient 连接管理 + 工具发现并注册进共享 `_REGISTRY` + `/tools` 自动含 MCP 工具 + 前端管理页 | 证明红线 #5 端到端打通 |
| B. capability token（后续） | 调用 MCP 工具时注入用户 scope（role/identity），经 contextvars 传递，让 MCP server 自行鉴权 | 加固（不泄漏原始身份） |
| C. HITL 审批闭环（后续） | destructiveHint 的 MCP 工具走人审 + admin 审批队列 | 破坏性操作人审 |

## 2. 架构：MCP 如何接入（透明集成）

现有工具治理链路**零改动**，MCP 工具只是新的 `ToolSpec` 来源：

```
┌─ 控制面 ─────────────────────────────────────────────────┐
│  MCPServer 实体（DB）  ← admin CRUD：注册 stdio/http server │
│  /api/admin/mcp-servers  + 连通性测试 + 热重载             │
└───────────────────────┬──────────────────────────────────┘
                        │ 启动 / reload 时
                        ▼
┌─ 数据面基础设施：mcp/ 包（新）──────────────────────────┐
│  MCPManager（单例，镜像 gateway/manager.ChannelManager） │
│   ├─ load_all(): 每个 enabled MCPServer → MCPClient      │
│   ├─ MCPClient: 后台 asyncio loop + ClientSession 持久连 │
│   │   discover tools → 每个 tool 注册成 ToolSpec 进       │
│   │   tools/registry._REGISTRY（name = mcp__{srv}__{tool}）│
│   └─ call_tool(): sync 桥（run_coroutine_threadsafe）     │
└───────────────────────┬──────────────────────────────────┘
                        │ 注册进同一张表
                        ▼
┌─ 治理脊柱（零改动）─────────────────────────────────────┐
│  CapabilitySet.allowed_tools → filter_tools（按白名单裁剪）│
│   → to_langchain_tools → graph ToolNode                   │
│  MCP 工具 = 普通工具名，RBAC「天然」生效                   │
└──────────────────────────────────────────────────────────┘
```

**关键不变量**：
- MCP 工具名空间化 `mcp__{server}__{tool}`，与内置工具不冲突。
- 治理脊柱对工具来源无感——`filter_tools` 只看 name 是否在白名单。
- 一个 MCP server 挂掉/禁用，只影响它自己的工具（其余工具照常）。

## 3. 关键设计决策

### D1. sync↔async 桥接
现有 `ToolSpec.func` 是 sync callable（LangGraph ToolNode 同步调用）；MCP `ClientSession.call_tool` 是 async。**方案**：每个 MCPClient 跑一个后台 daemon 线程的 asyncio loop，`ToolSpec.func` 通过 `asyncio.run_coroutine_threadsafe(call_tool(...), loop).result()` 阻塞等待。ClientSession 在连接生命周期内复用（不每次重连）。

### D2. 传输层：stdio + streamable HTTP
用官方 `mcp` SDK：stdio（本地子进程，command/args/env）+ streamable_http（远程，url/headers）。两者都是 MCP 标准传输。SSE 标记 legacy 不做。

### D3. 工具命名 + 前缀通配
- 命名：`mcp__{server_name}__{tool_name}`。
- `filter_tools` 增加前缀通配：白名单里 `mcp__sales__*` 匹配 sales server 全部工具（否则 admin 要逐个加，server 加新工具就漏）。`*` 仍代表全部。改动极小（gateway.py 一个函数）。

### D4. RBAC 零改动
MCP 工具只是 `ToolSpec` name，`RoleConfig.allowed_tools` 字符串白名单原样过滤。admin 的 `['*']` 含 MCP 工具。普通角色需 admin 显式把 MCP 工具名（或 `mcp__srv__*`）加进白名单才可见。

### D5. 凭证加密（红线 #8）
远程 MCP server 的 headers / stdio 的 env 可能含密钥。复用现有 Fernet（`governance/secrets.py`）：整段 env dict 与敏感 header 加密存库，API 只回脱敏。与 Model.api_key 同处理。

## 4. 数据模型：MCPServer（governance/models.py）

```python
class MCPServer(SQLModel, table=True):
    id: int | None (PK)
    name: str (唯一索引)          # 工具名前缀 mcp__{name}__
    description: str = ""
    transport: str                # "stdio" | "http"
    # stdio 传输
    command: str = ""             # 可执行命令
    args_json: str = "[]"         # JSON: list[str]，命令参数
    env_encrypted: str = ""       # Fernet 密文（JSON dict），API 脱敏
    # http 传输
    url: str = ""                 # streamable http endpoint
    headers_encrypted: str = ""   # Fernet 密文（JSON dict），API 脱敏
    is_enabled: bool = True
    created_at / updated_at
```

env/headers 整体加密（含敏感项的可能性高），与 Model.api_key 一致的 `enc:v1` 约定。

## 5. 包结构

```
src/bycall/mcp/                   # ★ 新包
├── __init__.py
├── client.py                     # MCPClient：一 server 一持久连接
│                                 #   后台 loop + ClientSession + discover + call_tool(sync)
└── manager.py                    # MCPManager 单例：load_all / reload / close_all
                                  #   镜像 gateway/manager.ChannelManager
```
- 不新建工具注册表——复用 `tools/registry._REGISTRY`。
- `tools/base.ToolSpec` 加 `source: str = "builtin"` 字段（默认内置），MCP 工具标 `"mcp"`，`/tools` 端点透出来源分组。
- `tools/gateway.filter_tools` 加前缀通配匹配。
- `api/app.py` 启动时 `MCPManager.load_all()`（在 IM 渠道连接之后）。

## 6. 实施切片（Slice A，本次）

| # | 任务 | 文件 | 验收 |
|---|------|------|------|
| A1 | 加 `mcp` SDK 依赖；`MCPServer` 实体 + seed | `pyproject.toml`, `governance/models.py`, `db.py` | 建表成功，单测过 |
| A2 | `MCPClient`：stdio+http 连接 / discover / sync call_tool / close | `mcp/client.py` | 单测：用 SDK 内存传输连一个最小 server，discover+call 成功 |
| A3 | `MCPManager` 单例：load_all / reload / close_all + 注册进 `_REGISTRY` | `mcp/manager.py` | 单测：注册后 `get_all_tools()` 含 MCP 工具 |
| A4 | `filter_tools` 前缀通配；`ToolSpec.source` 字段；`/tools` 透出来源 | `tools/gateway.py`, `tools/base.py`, `admin/api.py`, `admin/schemas.py` | 单测：`mcp__x__*` 匹配；admin 端工具列表带 source |
| A5 | admin CRUD：MCPServer 增删改查 + 连通性测试 + 热重载（凭证加密/脱敏） | `admin/api.py`, `admin/schemas.py` | 单测：CRUD + 凭证不回显 + test 连通 |
| A6 | app 启动接线 `MCPManager.load_all()`；shutdown close_all | `api/app.py` | 启动日志显示连接 MCP server |
| A7 | 前端 MCP 管理页 + api.js + 路由 + 菜单 | `frontend/src/views/MCP.vue`, `api.js`, `router.js`, `Layout.vue` | 浏览器：注册 server → 工具出现在 /tools → 角色白名单可选 |
| A8 | 端到端验证 + 全量测试绿色 | — | `make ci-local` 过；真实 stdio server 工具能被 LLM 调用 |

**端到端验收（Slice A）**：
1. admin 在控制台注册一个 stdio MCP server（如计算器）。
2. 「测试连接」成功，工具列表出现在 `/tools`（标 `mcp` 来源）。
3. admin 把 `mcp__calc__*` 加进某角色 `allowed_tools`。
4. 该角色用户提问 → LLM 调用该 MCP 工具 → 结果回填 → 审计落库。
5. 禁用该 server → 其工具从白名单候选消失，LLM 看不到。

## 7. 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 后台 loop 线程泄漏（reload/关闭时） | 中 | MCPClient.close() 先 cancel session 再 stop loop；单测断言线程退出 |
| MCP server 无响应挂死 call_tool | 中 | call_tool 带超时；超时返回错误字符串（不抛异常，让 LLM 重试/降级） |
| 工具名冲突（两个 server 同 tool 名） | 低 | `mcp__{server}__{tool}` 前缀天然隔离 |
| 测试需真实 MCP server | 中 | 用 SDK 内存传输（`create_connected_server_and_client_session`）免子进程 |

## 8. 待确认

1. **Slice 起步**：先做 Slice A（网关+注册表端到端），B/C 后续？—— 推荐。
2. **传输优先级**：stdio + http 都做（A2），还是先只 stdio？—— 推荐都做（SDK 抽象一致，增量小）。
3. **`/tools` 工具选择 UI**：现状是逗号分隔文本框。MCP 工具多了要不要升级成多选 + 分组？—— Slice A 先保持文本框 + 前缀通配，UI 升级列为 polish。

## 9. Slice C：HITL 审批完整闭环（已完成）

**一句话**：destructiveHint 的 MCP 工具调用人审拦截——LLM 调用时创建 pending 审批请求，
admin 审批通过后才实际执行，结果回写会话历史。

### 设计要点

**destructiveHint 捕获**：MCPClient 发现工具时读 `tool.annotations.destructiveHint`，
映射到 `ToolSpec.requires_approval`。

**审批门（func 层拦截）**：`to_langchain_tools` 对 `requires_approval=True` 的工具包装
审批门（`make_approval_gated_func`）——调用时不执行，而是创建 `ApprovalRequest(pending)`
并返回"待审批"消息给 LLM（LLM 告知用户等待）。

**审批上下文（contextvars）**：runner 在 graph 执行前设置 `ApprovalContext`
（user/conversation/channel），审批门读取并创建审批请求。与 capability token 平行。

**执行 + 闭环（无需 checkpointer）**：admin approve → `execute_approved_tool` 用存储的
参数实际执行（重新计算能力集验证权限仍在 → 设置 capability token → 调 spec.func + 脱敏），
结果存 `ApprovalRequest.result_text` + 追加到会话历史（用户下次交互可见）。与 ADR-0002 自洽。

**失败安全**：工具已被移除/用户权限已变更/用户已禁用 → 标记 failed/rejected，不崩。

### 新增文件

| 文件 | 职责 |
|------|------|
| `tools/approval.py` | 审批上下文（contextvars）+ 审批门 + 执行 + 模型操作 |
| `governance/models.py` `ApprovalRequest` | 审批请求实体（状态机 pending→executed/rejected/failed） |
| `admin/api.py` 审批端点 | GET /approvals（列表+过滤）/ approve / reject |
| `frontend/src/views/Approvals.vue` | 审批队列管理页（列表+过滤+审批+详情） |

### 关键不变量

治理脊柱零改动：审批门在 func 层，RBAC 白名单过滤不变（destructive 工具仍需在 allowed_tools 内才可见）。

审批执行时重新计算能力集：用户权限在审批等待期间可能已变更，执行时重新验证。

与 ADR-0002 自洽：不需要 LangGraph checkpointer/interrupt——审批门在 func 层返回"待审批"消息，
本轮对话正常结束；审批通过后的执行 + 结果回写是独立路径。
