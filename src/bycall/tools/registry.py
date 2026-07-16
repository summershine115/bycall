"""工具注册表：进程级全局 dict（工具名 → ToolSpec）。

"文件即扩展"模式（对齐 ``tools/__init__.py`` 占位注释与 OpenClaw 设计）：新增工具
只需定义 ``ToolSpec`` 并 ``register(...)``，或用 ``@register_tool`` 装饰一个函数，
工具即进入注册表，自动出现在控制面的工具目录与网关的过滤候选里。

为什么是进程级全局而非 DB 表（见 ADR-0002 / 计划 Step 3 决策）：
- 工具的"实现"是代码（func），本就无法存 DB——DB 只能存元数据。
- M3 不预置内置工具（只搭框架），后续工具按需在此注册。
- 权限（谁能用）仍在治理脊柱（``RoleConfig.allowed_tools`` 字符串白名单），
  注册表只回答"有哪些工具可选"，不回答"谁能用"。

M4 MCP 网关落地后，MCP 工具可在启动时动态注册进同一张表，对本注册表透明。
"""

from __future__ import annotations

from bycall.tools.base import ToolSpec

# 进程级全局注册表。模块导入即共享，无需单例类。
_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """注册一个工具规格。重名覆盖（后注册者生效，方便测试与热替换）。

    返回传入的 ``spec``，便于 ``register(ToolSpec(...))`` 一行式注册。
    """
    _REGISTRY[spec.name] = spec
    return spec


def register_tool(
    name: str,
    description: str,
    parameters: dict,
):
    """装饰器：把一个普通函数注册为工具。

    用法::

        @register_tool("get_time", "获取当前时间", {"type": "object", "properties": {}})
        def get_time() -> str:
            from datetime import datetime
            return datetime.now().isoformat()
    """

    def decorator(func):
        register(ToolSpec(name=name, description=description, parameters=parameters, func=func))
        return func

    return decorator


def get_tool(name: str) -> ToolSpec | None:
    """按名查工具。不存在返回 None。"""
    return _REGISTRY.get(name)


def get_all_tools() -> list[ToolSpec]:
    """返回全部已注册工具（按 name 排序，保证输出稳定）。"""
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def clear_registry() -> None:
    """清空注册表（仅供测试隔离用，生产勿调）。"""
    _REGISTRY.clear()


__all__ = ["clear_registry", "get_all_tools", "get_tool", "register", "register_tool"]
