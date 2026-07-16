"""tools 包 —— 工具与能力系统（文件即扩展）。

M3 工具治理的核心：让治理脊柱的 ``CapabilitySet.allowed_tools`` 真正被消费——
LLM 的工具列表 = 「该角色可用工具集」，无权工具 LLM 根本看不到。

三层结构：
- ``base.ToolSpec``：工具规格（name/description/参数 schema/执行函数），provider 无关。
- ``registry``：进程级全局注册表（``register`` / ``register_tool`` / ``get_all_tools``）。
  新增工具只需 ``register(ToolSpec(...))`` 或 ``@register_tool`` 装饰一个函数。
- ``gateway``：按 ``allowed_tools`` 白名单过滤 + 包成 LangChain ``StructuredTool``。

数据流：
  ``CapabilitySet.allowed_tools`` → ``gateway.filter_tools`` → ``gateway.to_langchain_tools``
    → ``agent/graph.py`` 的 ``ToolNode``（LLM 只见过滤后的列表）

M3 不预置内置工具（只搭框架）。后续工具按需在业务模块里 ``register``；
M4 MCP 网关落地后，MCP 工具在启动时动态注册进同一张表，对本包透明。
"""

from bycall.tools.base import ToolSpec
from bycall.tools.gateway import filter_tools, to_langchain_tools
from bycall.tools.registry import (
    clear_registry,
    get_all_tools,
    get_tool,
    register,
    register_tool,
)

__all__ = [
    "ToolSpec",
    "clear_registry",
    "filter_tools",
    "get_all_tools",
    "get_tool",
    "register",
    "register_tool",
    "to_langchain_tools",
]
