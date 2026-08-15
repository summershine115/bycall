"""参考场景包连接器测试（examples/connectors/）。

用 MCP SDK 内存传输（与 ``test_mcp_client.py`` 同模式）验证示例连接器的治理契约：

- 工具以 ``mcp__<server>__<tool>`` 全名被发现（进入 allowed_tools 白名单体系的前提）；
- ``destructiveHint=True`` → ``requires_approval=True``（审批门语义，红线 #5 / M4 Slice C）；
- 读/低风险写不触发审批门；
- 连接器侧角色复核（纵深防御）逻辑正确。

每个用例重新 importlib 加载模块——连接器是进程内存态，独立加载保证用例间状态隔离。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from bycall.mcp.client import MCPClient, MCPServerConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_connector(module_name: str):
    """从 examples/connectors/ 按文件路径加载连接器模块（examples 不是包）。"""
    path = _REPO_ROOT / "examples" / "connectors" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"example_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connect(server, name: str) -> MCPClient:
    """以内存传输连接 FastMCP server（免子进程）。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    client = MCPClient(
        MCPServerConfig(name=name, transport="stdio"),
        session_cm_factory=lambda: create_connected_server_and_client_session(server),
        connect_timeout=15,
        call_timeout=10,
    )
    client.connect()
    return client


# ---------- 工单连接器 ----------


def test_ticket_tools_discovered_with_approval_gate():
    mod = _load_connector("ticket_server")
    client = _connect(mod.server, "demo-ticket")
    try:
        tools = {t.name: t for t in client.tools}
    finally:
        client.close()

    assert set(tools) == {
        "mcp__demo-ticket__create_ticket",
        "mcp__demo-ticket__query_ticket",
        "mcp__demo-ticket__list_tickets",
        "mcp__demo-ticket__close_ticket",
    }
    # 高危（destructiveHint）→ 审批门；读/低风险写 → 直接放。
    assert tools["mcp__demo-ticket__close_ticket"].requires_approval is True
    for name in (
        "mcp__demo-ticket__create_ticket",
        "mcp__demo-ticket__query_ticket",
        "mcp__demo-ticket__list_tickets",
    ):
        assert tools[name].requires_approval is False


def test_ticket_create_query_roundtrip():
    mod = _load_connector("ticket_server")
    client = _connect(mod.server, "demo-ticket")
    try:
        created = json.loads(client.call_tool("create_ticket", {"title": "测试工单-泵体渗漏"}))
        assert created["ticket_id"].startswith("T-")

        detail = json.loads(client.call_tool("query_ticket", {"ticket_id": created["ticket_id"]}))
        assert detail["title"] == "测试工单-泵体渗漏"
        assert detail["status"] == "processing"
        # 无 meta（平台外直连）时身份降级为 unknown——LLM 参数无法伪造 created_by。
        assert detail["created_by"] == "unknown"

        listing = json.loads(client.call_tool("list_tickets", {"status": "processing"}))
        assert any(t["id"] == created["ticket_id"] for t in listing)
    finally:
        client.close()


def test_ticket_close_role_gate():
    mod = _load_connector("ticket_server")
    # 连接器侧角色复核：supervisor/admin 放行；其他角色拒绝；空身份（平台外直连）放行。
    assert mod._close_role_allowed("supervisor") is True
    assert mod._close_role_allowed("admin") is True
    assert mod._close_role_allowed("") is True
    assert mod._close_role_allowed("technician") is False
    assert mod._close_role_allowed("guest") is False

    # 无 meta 直连（role=""）→ 放行且确实关单（平台层治理已由白名单/审批门完成）。
    client = _connect(mod.server, "demo-ticket")
    try:
        closed = json.loads(
            client.call_tool("close_ticket", {"ticket_id": "T-1001", "resolution": "更换轴承"})
        )
        assert closed == {"ticket_id": "T-1001", "status": "closed"}
        again = client.call_tool("close_ticket", {"ticket_id": "T-1001", "resolution": "重复关单"})
        assert "已关闭" in again
    finally:
        client.close()


# ---------- 库存连接器 ----------


def test_inventory_search_and_stock():
    mod = _load_connector("inventory_server")
    client = _connect(mod.server, "demo-inventory")
    try:
        # 中文关键词（名称）命中。
        hits = json.loads(client.call_tool("search_parts", {"keyword": "轴承"}))
        assert [h["part_no"] for h in hits] == ["BRG-6205"]
        # 料号小写命中。
        hits = json.loads(client.call_tool("search_parts", {"keyword": "cat6"}))
        assert [h["part_no"] for h in hits] == ["NET-CAT6"]
        # 低于安全库存的物料带标记（SNS-PT100: 8 < 12）。
        hits = json.loads(client.call_tool("search_parts", {"keyword": "PT100"}))
        assert hits[0]["below_safety_stock"] is True

        detail = json.loads(client.call_tool("get_part_stock", {"part_no": "BRG-6205"}))
        assert detail["location"] == "A-01-03"
        assert client.call_tool("get_part_stock", {"part_no": "NOPE"}).startswith("[未找到]")
    finally:
        client.close()


def test_inventory_reserve_flow_and_gate():
    mod = _load_connector("inventory_server")
    client = _connect(mod.server, "demo-inventory")
    try:
        tools = {t.name: t for t in client.tools}
        assert tools["mcp__demo-inventory__reserve_part"].requires_approval is True
        assert tools["mcp__demo-inventory__search_parts"].requires_approval is False

        # 正常预留：扣减库存。
        ok = json.loads(
            client.call_tool(
                "reserve_part", {"part_no": "SNS-PT100", "qty": 3, "ticket_id": "T-1001"}
            )
        )
        assert ok["ok"] is True and ok["remain"] == 5
        # 超量：拒绝，不允许负库存。
        short = json.loads(client.call_tool("reserve_part", {"part_no": "SNS-PT100", "qty": 999}))
        assert short["ok"] is False and short["reason"] == "库存不足"
        # 非法数量。
        assert "qty" in client.call_tool("reserve_part", {"part_no": "SNS-PT100", "qty": 0})
    finally:
        client.close()
