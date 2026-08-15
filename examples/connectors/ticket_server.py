"""示例 MCP 连接器：demo 工单系统（stdio 传输）。

「参考场景包」的一部分（ADR-0009：平台场景无关，垂直场景由采用者自定义）。
本文件演示如何把一个业务系统封装成 MCP server 接入 Bycall：

- **零 Bycall 依赖**：只 import mcp SDK + 标准库，连接器完全可替换、可独立演进。
- **经 MCP 网关注册**（红线 #5）：admin/种子脚本登记 MCPServer 后，工具以
  ``mcp__demo-ticket__<tool>`` 进入 ``RoleConfig.allowed_tools`` 白名单体系，
  治理管道对 MCP 工具与内置工具一视同仁。
- **身份透传**：调用者身份从 ``ctx.request_context.meta`` 读取
  （``bycall_user_name`` / ``bycall_role``，由数据面 contextvars 注入，见
  ``src/bycall/mcp/context.py``）——连接器可据此自行审计/鉴权（纵深防御）。
- **风险分级示例**：读操作（query/list）放开；创建工单是低风险写，直接放；
  ``close_ticket`` 标 ``destructiveHint=True`` → Bycall 审批门自动拦截转 HITL
  人审（M4 Slice C），且连接器侧再校验角色（平台白名单之外的第二道防线）。

数据存进程内存（演示用，重启还原）。真实接入时把 ``_TICKETS`` 换成业务系统 API。

单独运行（stdio）：``python examples/connectors/ticket_server.py``
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("demo-ticket")

_LOCK = threading.Lock()  # MCP server 内并发调用防护（演示规范，真实实现按需）
_NEXT_ID = 1003

# 种子工单：覆盖「处理中 / 已完成」两种状态，方便演示查询过滤。
_TICKETS: dict[str, dict] = {
    "T-1001": {
        "id": "T-1001",
        "title": "3号产线传送带异响",
        "description": "低速运行时有周期性异响，疑似轴承磨损",
        "status": "processing",
        "priority": "high",
        "created_by": "demo-tech",
        "resolution": "",
    },
    "T-1002": {
        "id": "T-1002",
        "title": "会议室投影仪无信号",
        "description": "HDMI 接口接触不良",
        "status": "closed",
        "priority": "low",
        "created_by": "demo-guest",
        "resolution": "更换 HDMI 线缆",
    },
}

# close_ticket 的连接器侧角色下限（平台白名单之外的第二道防线；空身份 = 平台外
# 直连场景，放行，治理已由平台层完成）。
_CLOSE_MIN_ROLES = {"supervisor", "admin"}


def _meta_str(ctx: Context, key: str) -> str:
    """从 MCP 请求 meta 读 Bycall 注入的身份字段（缺失/异常返回空串）。

    Bycall 数据面调用工具时通过 ``meta`` 透传脱敏身份（见 ``mcp/context.py``），
    连接器自行决定是否/如何使用——本示例用它做审计打点与角色复核。
    """
    try:
        meta = getattr(ctx.request_context, "meta", None) or {}
        value = meta.get(key, "")
        return str(value) if value is not None else ""
    except Exception:  # 身份读取失败不应炸工具，降级为空
        return ""


def _close_role_allowed(role: str) -> bool:
    """close_ticket 的角色复核：supervisor/admin 放行，其余拒绝；空身份（平台外直连）放行。"""
    return role == "" or role in _CLOSE_MIN_ROLES


@server.tool()
def create_ticket(
    title: str, description: str = "", priority: str = "normal", ctx: Context = None
) -> str:
    """创建工单。priority 可选 low/normal/high/urgent。返回新工单号。"""
    if not title.strip():
        return "[错误] title 不能为空"
    with _LOCK:
        global _NEXT_ID
        ticket_id = f"T-{_NEXT_ID}"
        _NEXT_ID += 1
        _TICKETS[ticket_id] = {
            "id": ticket_id,
            "title": title.strip(),
            "description": description,
            "status": "processing",
            "priority": priority,
            # 身份透传：谁在调用由平台注入，不由 LLM 参数决定（不可伪造）。
            "created_by": _meta_str(ctx, "bycall_user_name") or "unknown",
            "resolution": "",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    return json.dumps({"ticket_id": ticket_id, "status": "processing"}, ensure_ascii=False)


@server.tool()
def query_ticket(ticket_id: str) -> str:
    """按工单号查工单详情。"""
    ticket = _TICKETS.get(ticket_id)
    if ticket is None:
        return f"[未找到] 工单 {ticket_id} 不存在"
    return json.dumps(ticket, ensure_ascii=False)


@server.tool()
def list_tickets(status: str = "") -> str:
    """列工单。status 可选过滤：processing / closed；空 = 全部。"""
    items = [t for t in _TICKETS.values() if status == "" or t["status"] == status]
    return json.dumps(
        [
            {"id": t["id"], "title": t["title"], "status": t["status"], "priority": t["priority"]}
            for t in items
        ],
        ensure_ascii=False,
    )


@server.tool(annotations=ToolAnnotations(destructiveHint=True))
def close_ticket(ticket_id: str, resolution: str, ctx: Context = None) -> str:
    """关闭工单（高危：终结流程且不可逆，需填写处理结果）。Bycall 侧会先走人工审批。

    连接器侧角色复核：即使白名单误配，非 supervisor/admin 也无法直接关单。
    """
    role = _meta_str(ctx, "bycall_role")
    if not _close_role_allowed(role):
        return f"[拒绝] 关闭工单需要 supervisor 及以上角色（当前 {role}）"
    ticket = _TICKETS.get(ticket_id)
    if ticket is None:
        return f"[未找到] 工单 {ticket_id} 不存在"
    if ticket["status"] == "closed":
        return f"[错误] 工单 {ticket_id} 已关闭"
    ticket["status"] = "closed"
    ticket["resolution"] = resolution
    ticket["closed_by"] = _meta_str(ctx, "bycall_user_name") or "unknown"
    return json.dumps({"ticket_id": ticket_id, "status": "closed"}, ensure_ascii=False)


def main() -> None:
    """stdio 模式入口（Bycall MCPManager 以子进程方式拉起）。"""
    server.run()


if __name__ == "__main__":
    main()
