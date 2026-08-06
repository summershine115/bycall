"""内置工具集合：开箱即用的基础工具，随包导入自动注册。

本模块在 ``tools/__init__.py`` 末尾被 import（注册触发点），任何 ``import bycall.tools``
都会连带把这里的工具注册进进程级 ``_REGISTRY``——REST /chat、IM 渠道、控制面
``GET /api/admin/tools`` 三条路径都覆盖（它们都间接 import 了 ``tools.gateway``，
而 gateway 被 ``tools/__init__`` 导入）。

设计目的（每个工具验证 M3 工具回路的一个特性）：

- ``current_time``：验证**无参工具**能进回路并执行（最简形态）。
- ``calculator``：验证**带必填参数**的工具——JSON Schema 参数经
  ``StructuredTool.from_function`` 反射调用 + 安全求值（防注入）。

权限（谁能用）不在本模块判定，由治理脊柱的 ``CapabilitySet.allowed_tools`` 白名单
决定（见 ``gateway.filter_tools``）。admin（``['*']``）自动可见全部；普通角色由
admin 在控制面把工具名加进 ``RoleConfig.allowed_tools``。
"""

from __future__ import annotations

import ast
import operator
from datetime import UTC, datetime
from typing import Any

from bycall.tools.base import ToolSpec
from bycall.tools.registry import register

# calculator 安全求值：只允许这些 AST 节点 + 双参运算符。
# 任何不在白名单的节点（名字调用、属性访问、import 等）一律拒绝，防代码注入。
_CALC_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CALC_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


# ============ 1. current_time：无参工具 ============


def _current_time() -> str:
    """返回当前 UTC 时间（ISO 8601 带时区）。

    用 UTC 而非本地时间，避免服务器时区差异导致 LLM 误读。LLM 如需本地时间可自行换算。
    """
    return datetime.now(UTC).isoformat()


register(
    ToolSpec(
        name="current_time",
        description='获取当前 UTC 时间（ISO 8601 格式）。当用户问"现在几点""今天日期"时调用。',
        parameters={"type": "object", "properties": {}, "required": []},
        func=_current_time,
    )
)


# ============ 2. calculator：带必填参数 + 安全求值 ============


def _safe_eval(expression: str) -> str:
    """对算术表达式做白名单 AST 求值。

    只允许数字字面量 + ``+ - * / // % **`` + 一元正负 + 括号。遇到任何不在白名单的
    节点（函数调用、属性访问、名字、布尔/比较运算符等）返回错误说明而非抛异常——
    工具执行错误应返回字符串（``ToolNode`` 会把它回喂给 LLM 让其重试或换思路，
    而不是把异常冒泡导致整条对话失败）。

    除零等运行时异常同样捕获返回错误说明。
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return f"表达式格式错误，无法解析：{expression!r}"

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):  # 数字字面量
            if isinstance(node.value, int | float):
                return node.value
            raise ValueError("不允许的字面量类型")
        if isinstance(node, ast.BinOp):
            op_fn = _CALC_BIN_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"不支持的运算符：{type(node.op).__name__}")
            return op_fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op_fn = _CALC_UNARY_OPS.get(type(node.op))
            if op_fn is None:
                raise ValueError(f"不支持的一元运算符：{type(node.op).__name__}")
            return op_fn(_eval(node.operand))
        raise ValueError(f"不允许的语法元素：{type(node).__name__}")

    try:
        result = _eval(tree)
    except ZeroDivisionError:
        return "错误：除零"
    except ValueError as e:
        return f"表达式包含不允许的内容：{e}"
    except Exception as e:
        return f"计算失败：{e}"

    # 整数结果去掉 .0 后缀，更自然（2.0 → "2"，2.5 → "2.5"）。
    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return str(result)


def _calculator(expression: str) -> str:
    """算术计算器入口。``expression`` 形如 ``"1 + 2 * 3"``。"""
    return _safe_eval(expression)


register(
    ToolSpec(
        name="calculator",
        description="算术计算器。支持加减乘除、取模、幂运算（+- * / // % **）。参数 expression 为算术表达式字符串。",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的算术表达式，例如 '1 + 2 * 3' 或 '(100-30)/2'",
                }
            },
            "required": ["expression"],
        },
        func=_calculator,
    )
)


# ============ 3. load_skill：渐进式披露——按需加载 skill 全文 ============


def _load_skill_raw(name: str) -> str:
    """返回标识——实际内容由 redactor 按 cap.on_demand_skills 返回。"""
    return name


def _load_skill_redactor(raw_name: str, cap) -> str:
    """按 cap.on_demand_skills 查找并返回 skill 全文。

    预过滤（红线 #1 落地）：``cap.on_demand_skills`` 仅含该用户绑定+启用+on_demand 的 skill，
    非绑定 skill 不在此列表，工具自然拿不到——不是"先给再拦"，而是"根本没给"。
    """
    for s in getattr(cap, "on_demand_skills", []):
        if s.name == raw_name:
            return s.content
    return f"未找到技能「{raw_name}」或你无权访问。可用的按需技能：{[s.name for s in getattr(cap, 'on_demand_skills', [])]}"


register(
    ToolSpec(
        name="load_skill",
        description=(
            "加载指定领域技能的完整知识文档。当需要详细参考某个技能的操作指南、话术或业务规则时调用。"
            "参数 name 为技能名（必填）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名（按需技能索引中列出的名称）",
                }
            },
            "required": ["name"],
        },
        func=_load_skill_raw,
        redactor=_load_skill_redactor,
    )
)


__all__ = ["_calculator", "_current_time", "_load_skill_raw", "_load_skill_redactor", "_safe_eval"]
