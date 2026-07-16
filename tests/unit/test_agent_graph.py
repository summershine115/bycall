"""LangGraph 工具回路单元测试 —— 验证 model → tool → model 循环（M3 工具治理）。

测试 ``agent.graph.run_tool_loop``：
- 有工具调用时：LLM 先返回 tool_calls → ToolNode 执行 → LLM 二轮返回最终回答。
- 无工具调用时：直接返回 LLM 回答（退化为纯对话）。
- 空工具集：不 bind_tools，单次 invoke。
- 迭代上限：超出后强制终止，给出降级提示。

用 mock ChatModel（返回预设的 AIMessage），不连真实 API。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from bycall.agent.graph import run_tool_loop


def _make_tool(name: str, reply: str = "tool_result") -> StructuredTool:
    """构造一个简单的 LangChain StructuredTool。"""
    return StructuredTool.from_function(
        name=name,
        description=f"测试工具 {name}",
        func=lambda **kwargs: reply,
    )


def _tool_call_msg(tool_name: str, call_id: str = "call_1", args: dict | None = None) -> AIMessage:
    """构造一个带 tool_calls 的 AIMessage（模拟 LLM 请求调工具）。"""
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": tool_name, "args": args or {}, "type": "tool_call"}],
    )


class TestRunToolLoop:
    def test_no_tools_single_invoke(self):
        """空工具集 → 退化为单次 LLM 调用，返回 answer。"""
        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.invoke.return_value = AIMessage(content="纯对话回答")

        result = run_tool_loop(
            chat_model=mock_model,
            tools=[],
            messages=[HumanMessage(content="你好")],
        )

        assert result.answer == "纯对话回答"
        assert result.tool_calls_made == []
        assert result.hit_iteration_limit is False

    def test_no_tool_calls_single_round(self):
        """有工具但 LLM 没调工具 → 单轮即止，返回 answer。"""
        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.bind_tools.return_value = mock_model
        mock_model.invoke.return_value = AIMessage(content="不需要工具就能回答")

        tool = _make_tool("get_time")
        result = run_tool_loop(
            chat_model=mock_model,
            tools=[tool],
            messages=[HumanMessage(content="你好")],
        )

        assert result.answer == "不需要工具就能回答"
        assert result.tool_calls_made == []

    def test_tool_call_then_final_answer(self):
        """完整回路：LLM 请求调工具 → 工具执行 → LLM 二轮给出最终回答。"""
        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.bind_tools.return_value = mock_model
        # 第一轮返回 tool_calls，第二轮返回最终文本。
        mock_model.invoke.side_effect = [
            _tool_call_msg("get_time"),
            AIMessage(content="现在是 12:00"),
        ]

        tool = _make_tool("get_time", reply="12:00")
        result = run_tool_loop(
            chat_model=mock_model,
            tools=[tool],
            messages=[HumanMessage(content="几点了？")],
        )

        assert result.answer == "现在是 12:00"
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["name"] == "get_time"
        # result = 工具返回值（本例无 redactor 原样），供审计采集
        assert result.tool_calls_made[0]["result"] == "12:00"
        assert result.hit_iteration_limit is False

    def test_tool_calls_recorded_for_audit(self):
        """多工具调用都被记录到 tool_calls_made（审计透明化）。"""
        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.bind_tools.return_value = mock_model
        mock_model.invoke.side_effect = [
            _tool_call_msg("get_time", call_id="c1"),
            _tool_call_msg("calc", call_id="c2"),
            AIMessage(content="完成"),
        ]

        tools = [_make_tool("get_time", "12:00"), _make_tool("calc", "42")]
        result = run_tool_loop(
            chat_model=mock_model,
            tools=tools,
            messages=[HumanMessage(content="几点？算个数")],
        )

        assert result.answer == "完成"
        assert [tc["name"] for tc in result.tool_calls_made] == ["get_time", "calc"]
        # 每条记录含 result（工具返回值，脱敏后；本例无 redactor 原样）
        assert [tc["result"] for tc in result.tool_calls_made] == ["12:00", "42"]

    def test_tool_result_truncated(self):
        """工具返回值超长被截断到 _RESULT_TRUNCATE（防爆库/防 token 膨胀）。"""
        from bycall.agent.graph import _RESULT_TRUNCATE

        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.bind_tools.return_value = mock_model
        mock_model.invoke.side_effect = [
            _tool_call_msg("big"),
            AIMessage(content="done"),
        ]
        tool = _make_tool("big", reply="x" * 2000)
        result = run_tool_loop(
            chat_model=mock_model,
            tools=[tool],
            messages=[HumanMessage(content="q")],
        )
        assert len(result.tool_calls_made) == 1
        assert len(result.tool_calls_made[0]["result"]) == _RESULT_TRUNCATE

    def test_iteration_limit_terminates(self):
        """触及迭代上限 → 强制终止，给出降级提示，hit_iteration_limit=True。"""
        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.bind_tools.return_value = mock_model
        # LLM 每次都返回 tool_calls（模拟死循环），由迭代上限拦下。
        # 用 side_effect 每次返回新对象，避免 LangGraph 消息 reducer 去重问题。
        mock_model.invoke.side_effect = lambda msgs: _tool_call_msg("get_time")

        tool = _make_tool("get_time")
        result = run_tool_loop(
            chat_model=mock_model,
            tools=[tool],
            messages=[HumanMessage(content="无限调工具")],
            max_iterations=2,
        )

        assert result.hit_iteration_limit is True
        assert "上限" in result.answer

    def test_final_answer_extracts_last_ai_content(self):
        """最终回复取末条 AIMessage.content。"""
        mock_model = MagicMock(spec=BaseChatModel)
        mock_model.bind_tools.return_value = mock_model
        mock_model.invoke.return_value = AIMessage(content="最终答案")

        result = run_tool_loop(
            chat_model=mock_model,
            tools=[_make_tool("x")],
            messages=[HumanMessage(content="问")],
        )

        assert result.answer == "最终答案"


# ============ _collect_tool_calls：审计重建（直接测纯函数）============


class TestCollectToolCalls:
    """``_collect_tool_calls`` 从消息序列重建工具调用记录，含「请求未执行」兜底。"""

    def test_normal_pair_collected(self):
        """正常配对：AIMessage(tool_calls) + ToolMessage → [{name,args,result}]。"""
        from bycall.agent.graph import _collect_tool_calls

        messages = [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "get_time", "args": {}, "type": "tool_call"}],
            ),
            ToolMessage(content="12:00", tool_call_id="c1", name="get_time"),
        ]
        calls = _collect_tool_calls(messages)
        assert len(calls) == 1
        assert calls[0] == {"name": "get_time", "args": {}, "result": "12:00"}

    def test_requested_but_not_executed_recorded(self):
        """核心审计论点：LLM 请求了 tool_call 但无 ToolMessage → 仍记为 not_executed。

        场景：ToolNode 因参数校验失败跳过了某个 call。若不兜底，该调用从审计蒸发。
        """
        from bycall.agent.graph import _collect_tool_calls

        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "send_email", "args": {"to": "x"}, "type": "tool_call"}
                ],
            ),
            # 注意：没有对应的 ToolMessage —— 工具未执行
        ]
        calls = _collect_tool_calls(messages)
        assert len(calls) == 1
        assert calls[0]["name"] == "send_email"
        assert calls[0]["args"] == {"to": "x"}
        assert calls[0]["result"] == "<not_executed>"

    def test_mixed_executed_and_not_executed(self):
        """混合：一个执行了，一个没执行 → 两条都记录，result 不同。"""
        from bycall.agent.graph import _collect_tool_calls

        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "get_time", "args": {}, "type": "tool_call"},
                    {"id": "c2", "name": "send_email", "args": {"to": "y"}, "type": "tool_call"},
                ],
            ),
            ToolMessage(content="12:00", tool_call_id="c1", name="get_time"),
            # c2 没有 ToolMessage
        ]
        calls = _collect_tool_calls(messages)
        assert len(calls) == 2
        by_name = {c["name"]: c for c in calls}
        assert by_name["get_time"]["result"] == "12:00"
        assert by_name["send_email"]["result"] == "<not_executed>"

    def test_empty_messages_returns_empty(self):
        from bycall.agent.graph import _collect_tool_calls

        assert _collect_tool_calls([]) == []
        assert _collect_tool_calls([HumanMessage(content="hi")]) == []
