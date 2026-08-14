"""工具网关：按治理脊柱的 ``allowed_tools`` 过滤工具集。

这是 product-design.md 支柱 3「工具管理」的落地——LLM 的工具列表 = 「该角色可用工具集」，
**无权工具 LLM 根本看不到**（不是"看到不让调"）。策略活在代码层，LLM 碰不到。

职责链：
  ``CapabilitySet.allowed_tools``（治理脊柱计算）
    → ``filter_tools``（本模块，按白名单裁剪候选）
    → ``to_langchain_tools``（包成 LangChain ``StructuredTool``）
    → 传给 ``agent/graph.py`` 的 ``ToolNode``（LLM 只见过滤后的列表）

关键语义（对齐 ``capability.py`` 的通配约定）：
- ``allowed_tools == ["*"]``（admin）→ 全部已注册工具。
- 普通用户 → 只保留 ``name in allowed_tools`` 的工具；列表里的工具名未注册则静默忽略
  （admin 可能配了一个还没实现的工具名，不应让对话崩）。
- 空列表 → 无工具（纯对话仍可进行，不阻塞；LLM 直接基于 RAG 上下文回答）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool

from bycall.tools.approval import make_approval_gated_func
from bycall.tools.base import ToolSpec
from bycall.tools.redactor import sanitize_tool_result
from bycall.tools.registry import get_all_tools

if TYPE_CHECKING:
    from bycall.governance.capability import CapabilitySet


def filter_tools(allowed_tools: list[str]) -> list[ToolSpec]:
    """按 ``allowed_tools`` 白名单过滤已注册工具。

    匹配规则（对齐 ``capability.py`` 的通配约定）：
    - ``["*"]`` → 全部（admin）。
    - 精确名（如 ``calculator``）→ name 精确匹配。
    - 前缀通配（如 ``mcp__sales__*``）→ 匹配所有以 ``mcp__sales__`` 开头的工具，
      方便 admin 一次性放行某 MCP server 的全部工具（server 加新工具也自动可见）。
    - 未注册的名字静默忽略（admin 可能配了还没实现的工具名，不应让对话崩）。
    - 空 → 空（纯对话）。

    返回按 name 排序的稳定列表。
    """
    all_tools = get_all_tools()
    if "*" in allowed_tools:
        return all_tools
    # 拆出精确名与前缀通配两类。
    exact = set(allowed_tools)
    prefixes = tuple(p[:-1] for p in allowed_tools if p.endswith("*") and len(p) > 1)

    def matches(name: str) -> bool:
        if name in exact:
            return True
        return any(name.startswith(prefix) for prefix in prefixes)

    return [t for t in all_tools if matches(t.name)]


def to_langchain_tools(specs: list[ToolSpec], cap: CapabilitySet) -> list[BaseTool]:
    """把 ``ToolSpec`` 列表包成 LangChain ``StructuredTool`` 供 ``ToolNode`` 消费。

    ``StructuredTool`` 按 ``parameters``（JSON Schema）反射调用 ``func``。
    空列表返回空列表（graph 的 agent node 不 bind tools，退化为纯对话）。

    ``cap`` 是当前会话能力集，闭包注入每个工具的脱敏包装（支柱 4 第二条泄漏面）：
    声明了 ``redactor`` 的工具，返回值经 ``redactor(raw, cap)`` 脱敏后才进 LLM 上下文；
    未声明的原样透传。``CapabilitySet`` 透传到此处即不再回查 DB。

    HITL 审批门（M4 Slice C）：``requires_approval=True`` 的工具（MCP destructiveHint）
    不走脱敏包装，改走审批门（``make_approval_gated_func``）——调用时不执行，而是创建
    ``ApprovalRequest(pending)`` 并返回"待审批"消息给 LLM。审批通过后的实际执行 +
    脱敏在 ``execute_approved_tool`` 中独立处理（绕过 StructuredTool 直接调 spec.func）。
    """
    tools: list[BaseTool] = []
    for spec in specs:
        if spec.requires_approval:
            func = make_approval_gated_func(spec)
        else:
            func = _wrap_with_redactor(spec, cap)
        tools.append(
            StructuredTool.from_function(
                name=spec.name,
                description=spec.description,
                args_schema=_to_pydantic_schema(spec.name, spec.parameters),
                func=func,
            )
        )
    return tools


def _wrap_with_redactor(spec: ToolSpec, cap: CapabilitySet):
    """构造工具 ``func`` 的脱敏包装。

    - ``spec.redactor is None`` → 原样返回 ``spec.func``（零开销，无脱敏）。
    - 否则返回 ``wrapped(**kwargs) = redactor(spec.func(**kwargs), cap)``，闭包捕获
      ``cap`` 与 ``redactor``。

    ``StructuredTool.from_function`` 按 ``args_schema`` 字段名反射调用，故包装函数用
    ``**kwargs`` 解包给原 ``func``（字段名与 func 参数名对齐是 ToolSpec 契约）。脱敏后
    的值经 ``ToolNode`` 的 ``msg_content_output`` 原样（str）进 ``ToolMessage.content``，
    供下一轮 LLM 上下文 + 审计采集——LLM 与审计看到的都是脱敏后值。
    """
    redactor = spec.redactor
    orig = spec.func

    def wrapped(**kwargs):
        # 统一脱敏 + 截断管道（缝隙 #5）：无论是否声明 redactor 都过 sanitize_tool_result。
        return sanitize_tool_result(orig(**kwargs), cap, redactor=redactor)

    return wrapped


def _to_pydantic_schema(name: str, parameters: dict):
    """把 JSON Schema dict 转成 Pydantic 模型（StructuredTool 需要 args_schema）。

    LangChain ``StructuredTool.from_function`` 的 ``args_schema`` 接受 Pydantic
    BaseModel 或 None。我们把工具的 JSON Schema 参数包成一个动态 Pydantic 模型，
    字段从 ``properties`` / ``required`` 反射生成。
    """
    from pydantic import create_model

    properties: dict = parameters.get("properties", {})
    required: list[str] = parameters.get("required", [])
    fields: dict = {}
    for field_name, field_schema in properties.items():
        # JSON Schema type → Python type 的粗映射；复杂 schema 退化为 Any。
        py_type = _json_type_to_python(field_schema.get("type"))
        default = ... if field_name in required else None
        fields[field_name] = (py_type, default)

    return create_model(f"{name}_ArgsSchema", **fields)


def _json_type_to_python(json_type: str | None):
    """JSON Schema type → Python type 粗映射。未知/复杂类型退化为 Any。"""
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    if json_type is None:
        return str | None
    return mapping.get(json_type, str | None)


__all__ = ["filter_tools", "to_langchain_tools"]
