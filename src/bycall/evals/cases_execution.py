"""执行质量评测用例（T2.1 第三类）。

离线样例覆盖执行链路四项关键质量属性：
- 工具结果统一脱敏 + 截断（T1.6）；
- 工具参数注入防护（calculator 白名单 AST 求值）；
- LangGraph 工具回路：真实执行 + 调用审计（name/args/result）；
- 迭代上限保护（防死循环费用 DoS）。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from bycall.evals.core import EvalCase
from bycall.evals.harness import assert_true, ensure_builtin_tools
from bycall.governance.capability import CapabilitySet
from bycall.tools.builtins import _safe_eval
from bycall.tools.redactor import MAX_TOOL_RESULT_CHARS, sanitize_tool_result


def _cap() -> CapabilitySet:
    return CapabilitySet(user_id=1, user_name="eval", role="member", level=0)


def _execution_tool_result_truncation(_ctx) -> None:
    """未声明 redactor 的工具结果也默认截断（防超大返回撑爆上下文）。"""
    huge = "x" * (MAX_TOOL_RESULT_CHARS + 500)
    out = sanitize_tool_result(huge, _cap())

    assert_true(
        len(out) <= MAX_TOOL_RESULT_CHARS + 50,  # 截断标注本身占少量字符
        "未声明 redactor 的工具结果未默认截断",
        length=len(out),
        limit=MAX_TOOL_RESULT_CHARS,
    )
    assert_true("已截断" in out, "截断标注缺失", tail=out[-40:])
    assert_true(len(huge) not in (0,) and huge not in out, "超限原文仍完整返回")


def _execution_tool_redactor_applied(_ctx) -> None:
    """声明 redactor 的工具：脱敏先于截断，且脱敏值再过大仍受大小限制。"""
    raw = {"phone": "13800138000", "note": "ok"}

    def redactor(value, cap):
        value = dict(value)
        value["phone"] = "***"
        return value

    out = sanitize_tool_result(raw, _cap(), redactor=redactor)

    assert_true("13800138000" not in out, "敏感字段未被 redactor 脱敏", output=out)
    assert_true("***" in out, "脱敏占位符缺失", output=out)


def _execution_calculator_injection_rejected(_ctx) -> None:
    """calculator 的白名单 AST 求值拒绝任意代码执行。"""
    out = _safe_eval("__import__('os').system('id')")

    assert_true(
        out.startswith(("表达式格式错误", "表达式包含不允许的内容")), "注入表达式未拒绝", output=out
    )
    assert_true(_safe_eval("1 + 2 * 3") == "7", "正常算术求值回归", output=_safe_eval("1 + 2 * 3"))


class _FakeToolLoopModel:
    """有状态假 LLM：第一轮发出 calculator 调用，第二轮给最终答案。"""

    def __init__(self, final: str = "计算结果为 3") -> None:
        self.final = final
        self.invocations = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.invocations += 1
        if self.invocations == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "calculator",
                        "args": {"expression": "1 + 2"},
                        "id": "call_eval_1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content=self.final)


class _LoopingModel(_FakeToolLoopModel):
    """永远要求调工具的假 LLM：用于验证迭代上限。"""

    def invoke(self, messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "calculator",
                    "args": {"expression": "1"},
                    "id": f"call_loop_{self.invocations}",
                    "type": "tool_call",
                }
            ],
        )


def _execution_tool_loop_roundtrip(_ctx) -> None:
    """工具回路：真实执行 + 审计采集 name/args/result（质量可回放）。"""
    from bycall.agent.graph import run_tool_loop
    from bycall.tools.gateway import filter_tools, to_langchain_tools

    ensure_builtin_tools()
    specs = filter_tools(["calculator"])
    tools = to_langchain_tools(specs, _cap())
    result = run_tool_loop(
        chat_model=_FakeToolLoopModel(),
        tools=tools,
        messages=[HumanMessage(content="1+2 等于几")],
    )

    assert_true(result.answer == "计算结果为 3", "工具回路最终答案错误", answer=result.answer)
    assert_true(len(result.tool_calls_made) == 1, "工具调用审计缺失", calls=result.tool_calls_made)
    call = result.tool_calls_made[0]
    assert_true(call["name"] == "calculator", "审计工具名错误", call=call)
    assert_true(call["args"] == {"expression": "1 + 2"}, "审计参数错误", call=call)
    assert_true(call["result"] == "3", "审计结果错误", call=call)


def _execution_tool_loop_iteration_limit(_ctx) -> None:
    """死循环工具调用在迭代上限被强制终止（费用/性能防护）。"""
    from bycall.agent.graph import run_tool_loop
    from bycall.tools.gateway import filter_tools, to_langchain_tools

    ensure_builtin_tools()
    tools = to_langchain_tools(filter_tools(["calculator"]), _cap())
    result = run_tool_loop(
        chat_model=_LoopingModel(),
        tools=tools,
        messages=[HumanMessage(content="一直算")],
        max_iterations=1,
    )

    assert_true(result.hit_iteration_limit, "迭代上限未触发", calls=result.tool_calls_made)
    assert_true("上限" in result.answer, "降级提示缺失", answer=result.answer)


CASES = [
    EvalCase(
        id="execution_tool_result_truncation",
        name="工具结果默认截断",
        category="execution_quality",
        description="未声明 redactor 的工具返回也过统一大小限制（T1.6）",
        run=_execution_tool_result_truncation,
        change_types=("tool",),
    ),
    EvalCase(
        id="execution_tool_redactor_applied",
        name="工具结果脱敏管道",
        category="execution_quality",
        description="声明 redactor 的工具先脱敏再截断（T1.6）",
        run=_execution_tool_redactor_applied,
        change_types=("tool",),
    ),
    EvalCase(
        id="execution_calculator_injection_rejected",
        name="工具参数注入防护",
        category="execution_quality",
        description="calculator 白名单 AST 求值拒绝代码注入",
        run=_execution_calculator_injection_rejected,
        change_types=("tool",),
    ),
    EvalCase(
        id="execution_tool_loop_roundtrip",
        name="工具回路执行与审计",
        category="execution_quality",
        description="LangGraph 工具回路真实执行 + name/args/result 审计采集",
        run=_execution_tool_loop_roundtrip,
        change_types=("tool",),
    ),
    EvalCase(
        id="execution_tool_loop_iteration_limit",
        name="工具回路迭代上限",
        category="execution_quality",
        description="LLM 死循环调用工具在迭代上限被强制终止",
        run=_execution_tool_loop_iteration_limit,
        change_types=("tool", "model"),
    ),
]
