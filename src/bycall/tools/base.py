"""工具规格：工具的 provider 无关描述。

一个 ``ToolSpec`` 描述一个工具的"对外契约"——LLM 看到的 name/description/参数
schema，以及实际执行函数 ``func``。工具网关（``gateway.py``）据此过滤 + 包装成
LangChain ``StructuredTool`` 供 ``ToolNode`` 消费。

设计要点：
- ``parameters`` 是 JSON Schema（OpenAI function calling 惯例），由注册者提供。
- ``func`` 是普通 callable，签名应与 ``parameters`` 对齐（``StructuredTool`` 会按
  schema 反射调用）。工具执行错误应返回字符串说明而非抛异常（``ToolNode`` 会把异常
  内容回喂给 LLM 让它重试）。
- ``redactor``（可选）是工具返回值的脱敏函数——支柱 4 第二条泄漏面的落地：
  ``redactor(raw_result, capability_set) -> 脱敏后值``。工具网关在包装 ``func`` 时
  闭包注入当前 ``CapabilitySet``，工具返回值脱敏**后**才进 LLM 上下文（也才进审计）。
  返回值最好是 ``str``（``ToolMessage`` 对 str 原样透传）。``None``（默认）= 不脱敏，
  原样透传——M3 框架先行，工具按需声明脱敏。
- 不在此处绑定权限——权限是治理脊柱的事（``CapabilitySet.allowed_tools``），工具
  本身只描述"能做什么"，"谁能用"由网关在过滤时判定。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """工具规格：name + description + 参数 schema + 执行函数。

    - ``name``：工具唯一标识，与 ``RoleConfig.allowed_tools`` / ``CapabilitySet.allowed_tools``
      的字符串匹配。LLM 在 function calling 时用这个名字。
    - ``description``：给 LLM 看的自然语言描述（决定 LLM 何时选这个工具）。
    - ``parameters``：JSON Schema dict，描述 ``func`` 的入参。
    - ``func``：实际执行函数。参数由 LLM 的 tool_call.args 提供。
    - ``redactor``：可选脱敏函数 ``redactor(raw_result, capability_set) -> 脱敏后值``。
      见模块 docstring（支柱 4 第二条泄漏面）。``None`` = 不脱敏，原样透传。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    redactor: Callable[..., Any] | None = None
    # 工具来源："builtin"（内置）/ "mcp"（MCP 网关注入）。供 /tools 端点分组展示，
    # 不参与权限判定（谁能用仍由治理脊柱的 allowed_tools 白名单决定）。
    source: str = "builtin"
    # 是否需要人审（HITL）：MCP 工具的 ``destructiveHint=true`` 或内置工具显式标记时为 True。
    # 工具网关的审批门拦截此类工具的执行——不直接调用，而是创建 ApprovalRequest（pending），
    # 返回"待审批"消息给 LLM。admin 审批通过后才实际执行（M4 Slice C 完整闭环）。
    requires_approval: bool = False


__all__ = ["ToolSpec"]
