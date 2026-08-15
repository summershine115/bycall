"""示例 MCP 连接器：demo 备件/物料库存系统（stdio 传输）。

「参考场景包」的一部分（ADR-0009：平台场景无关，垂直场景由采用者自定义）。
与 ``ticket_server.py`` 互补，本文件演示的侧重点：

- **只读工具直接放开**（search/get）：查询类无副作用，白名单内即可用；
- **状态变更工具标 ``destructiveHint=True``**（``reserve_part`` 扣减可用库存）：
  Bycall 审批门拦截转 HITL 人审——与工单连接器的 ``close_ticket`` 同一机制，
  说明「高危 = 需人审」是平台统一语义，连接器只需声明。
- 种子物料刻意跨行业混排（工业备件 + IT 配件），强调这是**场景无关**的示例数据，
  采用者应替换为自己的物料主数据。

数据存进程内存（演示用，重启还原）。真实接入时把 ``_PARTS`` 换成 ERP/WMS API。

单独运行（stdio）：``python examples/connectors/inventory_server.py``
"""

from __future__ import annotations

import json
import threading

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("demo-inventory")

_LOCK = threading.Lock()

# 种子物料主数据：part_no 唯一；跨行业混排体现场景无关。
_PARTS: dict[str, dict] = {
    "BRG-6205": {
        "part_no": "BRG-6205",
        "name": "滚动轴承 6205-2RS",
        "category": "传动件",
        "location": "A-01-03",
        "stock": 42,
        "safety_stock": 10,
        "unit": "个",
        "in_transit": 0,
    },
    "SNS-PT100": {
        "part_no": "SNS-PT100",
        "name": "温度传感器 PT100",
        "category": "仪表",
        "location": "A-02-11",
        "stock": 8,
        "safety_stock": 12,
        "unit": "支",
        "in_transit": 20,
    },
    "FLT-HF500": {
        "part_no": "FLT-HF500",
        "name": "液压滤芯 HF-500",
        "category": "液压",
        "location": "B-01-05",
        "stock": 25,
        "safety_stock": 6,
        "unit": "个",
        "in_transit": 0,
    },
    "CBL-ENC3M": {
        "part_no": "CBL-ENC3M",
        "name": "伺服编码器线缆 3m",
        "category": "电气",
        "location": "B-03-01",
        "stock": 3,
        "safety_stock": 5,
        "unit": "根",
        "in_transit": 10,
    },
    "MEM-16G": {
        "part_no": "MEM-16G",
        "name": "笔记本内存条 16G DDR5",
        "category": "IT 配件",
        "location": "C-01-02",
        "stock": 15,
        "safety_stock": 4,
        "unit": "条",
        "in_transit": 0,
    },
    "SSD-1T": {
        "part_no": "SSD-1T",
        "name": "固态硬盘 1TB NVMe",
        "category": "IT 配件",
        "location": "C-01-03",
        "stock": 6,
        "safety_stock": 3,
        "unit": "块",
        "in_transit": 0,
    },
    "NET-CAT6": {
        "part_no": "NET-CAT6",
        "name": "网线 Cat6 3m",
        "category": "IT 配件",
        "location": "C-02-01",
        "stock": 60,
        "safety_stock": 20,
        "unit": "根",
        "in_transit": 0,
    },
    "KBD-100": {
        "part_no": "KBD-100",
        "name": "有线机械键盘",
        "category": "IT 配件",
        "location": "C-02-02",
        "stock": 2,
        "safety_stock": 2,
        "unit": "把",
        "in_transit": 0,
    },
}


def _meta_str(ctx: Context, key: str) -> str:
    """从 MCP 请求 meta 读 Bycall 注入的身份字段（缺失/异常返回空串）。"""
    try:
        meta = getattr(ctx.request_context, "meta", None) or {}
        value = meta.get(key, "")
        return str(value) if value is not None else ""
    except Exception:  # 身份读取失败降级为空，不炸工具
        return ""


def _summary(part: dict) -> dict:
    """列表/检索返回的精简视图（不含库位等仓储细节）。"""
    low = part["stock"] < part["safety_stock"]
    return {
        "part_no": part["part_no"],
        "name": part["name"],
        "category": part["category"],
        "stock": part["stock"],
        "unit": part["unit"],
        "below_safety_stock": low,
        "in_transit": part["in_transit"],
    }


@server.tool()
def search_parts(keyword: str) -> str:
    """按关键词搜备件（匹配名称/料号/类别），返回库存概况与是否低于安全库存。"""
    kw = keyword.strip().lower()
    if not kw:
        return "[错误] keyword 不能为空"
    hits = [
        _summary(p)
        for p in _PARTS.values()
        if kw in p["name"].lower() or kw in p["part_no"].lower() or kw in p["category"].lower()
    ]
    if not hits:
        return f"[未找到] 没有匹配「{keyword}」的物料"
    return json.dumps(hits, ensure_ascii=False)


@server.tool()
def get_part_stock(part_no: str) -> str:
    """按精确料号查库存详情（含库位、安全库存、在途）。"""
    part = _PARTS.get(part_no)
    if part is None:
        return f"[未找到] 物料 {part_no} 不存在"
    return json.dumps(part, ensure_ascii=False)


@server.tool(annotations=ToolAnnotations(destructiveHint=True))
def reserve_part(part_no: str, qty: int, ticket_id: str = "", ctx: Context = None) -> str:
    """预留备件（扣减可用库存，高危：影响可用量）。Bycall 侧会先走人工审批。

    qty 必须为正整数；库存不足时拒绝（不允许负库存）。
    """
    who = _meta_str(ctx, "bycall_user_name")
    part = _PARTS.get(part_no)
    if part is None:
        return f"[未找到] 物料 {part_no} 不存在"
    if qty <= 0:
        return "[错误] qty 必须为正整数"
    with _LOCK:
        if part["stock"] < qty:
            return json.dumps(
                {"ok": False, "reason": "库存不足", "available": part["stock"]}, ensure_ascii=False
            )
        part["stock"] -= qty
    return json.dumps(
        {
            "ok": True,
            "part_no": part_no,
            "reserved": qty,
            "remain": part["stock"],
            "for_ticket": ticket_id,
            "reserved_by": who or "unknown",
        },
        ensure_ascii=False,
    )


def main() -> None:
    """stdio 模式入口（Bycall MCPManager 以子进程方式拉起）。"""
    server.run()


if __name__ == "__main__":
    main()
