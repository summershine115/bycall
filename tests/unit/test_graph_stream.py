"""``run_tool_loop_stream`` 单元测试 —— 验证真正的流式工具回路。

关键论点（相对旧实现 graph.invoke + 重跑 astream 的回归保护）：
- 最终答案只 astream 一次（``astream`` 调用次数 = 工具回合数 + 1，无额外重跑）。
- 工具回合的文本不外送（不污染 ``run_agent_stream`` 累积进 DB/审计的 answer）。
- 工具调用审计记录（``tool_calls_made``）正确填充。
- 无工具路径逐 token yield；触及迭代上限 yield 降级提示。

用 mock ChatModel：``astream`` 为异步生成器，逐 token yield ``AIMessageChunk``。
工具回合的 chunk 带 ``tool_call_chunks``（content 空），最终答案回合的 chunk 带 content。
``bind_tools`` 返回 mock 自身。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.tools import StructuredTool

from bycall.agent.graph import TOOL_ROUND_FLUSH, GraphResult, run_tool_loop_stream


def _make_tool(name: str, reply: str = "tool_result") -> StructuredTool:
    """构造一个接受任意 kwargs 的简单 StructuredTool（与 test_agent_graph 同构）。"""

    def _fn(**kwargs):
        return reply

    return StructuredTool.from_function(name=name, description=f"测试工具 {name}", func=_fn)


def _tc_chunk(name: str, args_json: str = "{}", call_id: str = "call_1", index: int = 0):
    """单个工具调用 chunk：带 tool_call_chunks、content 空。"""
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": name,
                "args": args_json,
                "id": call_id,
                "index": index,
                "type": "tool_call_chunk",
            }
        ],
    )


def _text_chunks(text: str):
    """把文本拆成逐字符的 AIMessageChunk（模拟 token-by-token 流式）。"""
    return [AIMessageChunk(content=ch) for ch in text]


def _make_stream_mock_llm(rounds: list[list[AIMessageChunk]]):
    """构造 mock ChatModel，``astream`` 按 rounds 顺序逐轮 yield。

    - ``bind_tools`` 返回自身（有工具路径用 bind_tools 后的对象再 astream）。
    - 每次 ``astream`` 调用消耗一轮；超出范围时复用最后一轮（迭代上限场景）。
    - 返回 (mock, calls)，``calls["n"]`` 记录 astream 实际调用次数（供「不重跑」断言）。
    """
    m = MagicMock(spec=BaseChatModel)
    m.bind_tools.return_value = m
    calls = {"n": 0}

    async def _astream(messages, **kwargs):
        idx = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        for ch in rounds[idx]:
            yield ch

    m.astream = _astream
    return m, calls


async def _collect(stream):
    """把异步生成器的 yield 累积成 (增量文本列表, 最终 GraphResult)。"""
    chunks: list[str] = []
    final: GraphResult | None = None
    async for text, is_final, gr in stream:
        if is_final:
            final = gr
        elif text is TOOL_ROUND_FLUSH:
            chunks.clear()  # 工具回合前导语丢弃
        elif text:
            chunks.append(text)
    return chunks, final


# ============ 无工具路径：真流式 ============


@pytest.mark.asyncio
async def test_stream_no_tools_yields_token_by_token():
    """无工具 → 逐 token yield，GraphResult.answer 正确，astream 仅调一次。"""
    mock, calls = _make_stream_mock_llm([_text_chunks("你好")])

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=[],
        messages=[HumanMessage(content="你好")],
    )
    chunks, final = await _collect(stream)

    assert chunks == ["你", "好"]
    assert final is not None
    assert final.answer == "你好"
    assert final.tool_calls_made == []
    assert final.hit_iteration_limit is False
    # 无工具路径不重跑：astream 恰好一次。
    assert calls["n"] == 1


# ============ 有工具路径：完整回路 + 不重跑 ============


@pytest.mark.asyncio
async def test_stream_with_tools_executes_then_streams_answer():
    """工具回合执行后最终答案流式 yield；tool_calls_made 填充正确；不重跑。"""
    tool = _make_tool("get_time", reply="12:00")
    # 第一轮：工具调用；第二轮：最终答案。
    rounds = [
        [_tc_chunk("get_time", args_json='{"q": "now"}', call_id="c1")],
        _text_chunks("现在是 12:00"),
    ]
    mock, calls = _make_stream_mock_llm(rounds)

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=[tool],
        messages=[HumanMessage(content="几点了？")],
    )
    chunks, final = await _collect(stream)

    # 最终答案逐 token yield。
    assert "".join(chunks) == "现在是 12:00"
    assert final is not None
    assert final.answer == "现在是 12:00"
    # 工具调用审计：name/result 正确，args 由 tool_call_chunks 的 JSON 解析而来。
    assert len(final.tool_calls_made) == 1
    tc = final.tool_calls_made[0]
    assert tc["name"] == "get_time"
    assert tc["args"] == {"q": "now"}
    assert tc["result"] == "12:00"
    assert final.hit_iteration_limit is False
    # 核心回归点：astream 仅 2 次（1 次工具回合 + 1 次最终答案），旧实现会多 1 次重跑。
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_stream_with_tools_bind_tools_called_once():
    """bind_tools 只调一次（回路复用同一个 bind 后的模型）。"""
    tool = _make_tool("get_time", reply="12:00")
    rounds = [
        [_tc_chunk("get_time", call_id="c1")],
        _text_chunks("done"),
    ]
    mock, _ = _make_stream_mock_llm(rounds)

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=[tool],
        messages=[HumanMessage(content="q")],
    )
    await _collect(stream)

    assert mock.bind_tools.call_count == 1


# ============ 工具回合文本不外送 ============


@pytest.mark.asyncio
async def test_stream_tool_round_text_not_yielded():
    """工具回合若 LLM 输出了文本，该文本不 yield（只 yield 最终答案）。

    工具回合第一个 chunk 带 tool_call_chunks → round_kind 判为 "tool"，
    后续即便出现 content 文本也被抑制（不污染 runner 累积的 full_answer）。
    """
    tool = _make_tool("get_time", reply="12:00")
    # 工具回合：先 tool_call_chunk，再一段「思考中」文本——应被抑制。
    rounds = [
        [
            _tc_chunk("get_time", call_id="c1"),
            AIMessageChunk(content="正在查询时间"),
        ],
        _text_chunks("12:00"),
    ]
    mock, _ = _make_stream_mock_llm(rounds)

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=[tool],
        messages=[HumanMessage(content="几点了？")],
    )
    chunks, final = await _collect(stream)

    yielded = "".join(chunks)
    assert yielded == "12:00"
    assert "正在查询时间" not in yielded
    assert final is not None
    assert final.answer == "12:00"
    # 工具仍被执行。
    assert len(final.tool_calls_made) == 1
    assert final.tool_calls_made[0]["result"] == "12:00"


@pytest.mark.asyncio
async def test_stream_text_before_tool_call_still_executes_tool():
    """模型先输出前导文本再发 tool_call 时，工具仍执行、后续答案正常 yield。

    回归「企微端回复卡在前导语、后半段不显示」：旧实现按首 chunk 判回合类型——
    首 chunk 是文本 → 误判为答案回合，把前导语（如「当前 UTC 时间是：」）当最终答案
    yield 并 return，工具调用被丢弃、后续真实答案永不生成。修复后以「组装后是否含
    tool_calls」为权威判定，前导语经 TOOL_ROUND_FLUSH 丢弃。
    """
    tool = _make_tool("get_time", reply="12:00")
    # 工具回合：先一段前导文本，再 tool_call_chunk（模拟模型边说边决定调工具）。
    rounds = [
        [*_text_chunks("当前 UTC 时间是："), _tc_chunk("get_time", call_id="c1")],
        _text_chunks("当前 UTC 时间是：12:00"),
    ]
    mock, calls = _make_stream_mock_llm(rounds)

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=[tool],
        messages=[HumanMessage(content="几点了？")],
    )
    chunks, final = await _collect(stream)

    yielded = "".join(chunks)
    # 最终答案是第二轮的真实答案；前导语已被 TOOL_ROUND_FLUSH 丢弃，不重复/不污染。
    assert yielded == "当前 UTC 时间是：12:00"
    assert final is not None
    assert final.answer == "当前 UTC 时间是：12:00"
    # 工具被执行（旧 bug 会跳过工具直接 return 前导语）。
    assert len(final.tool_calls_made) == 1
    assert final.tool_calls_made[0]["name"] == "get_time"
    assert final.tool_calls_made[0]["result"] == "12:00"
    # 跑了两轮（工具回合 + 答案回合），非一轮提前终止。
    assert calls["n"] == 2


# ============ 多工具调用 ============


@pytest.mark.asyncio
async def test_stream_multiple_tool_calls_recorded():
    """一轮里多个 tool_calls 都执行并记入审计。"""
    tools = [_make_tool("get_time", reply="12:00"), _make_tool("calc", reply="42")]
    rounds = [
        [
            _tc_chunk("get_time", args_json='{"q": "now"}', call_id="c1", index=0),
            _tc_chunk("calc", args_json='{"expr": "6*7"}', call_id="c2", index=1),
        ],
        _text_chunks("完成"),
    ]
    mock, _ = _make_stream_mock_llm(rounds)

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=tools,
        messages=[HumanMessage(content="几点？算个数")],
    )
    chunks, final = await _collect(stream)

    assert "".join(chunks) == "完成"
    assert final is not None
    by_name = {tc["name"]: tc for tc in final.tool_calls_made}
    assert set(by_name) == {"get_time", "calc"}
    assert by_name["get_time"]["result"] == "12:00"
    assert by_name["calc"]["result"] == "42"
    assert by_name["get_time"]["args"] == {"q": "now"}
    assert by_name["calc"]["args"] == {"expr": "6*7"}


# ============ 迭代上限 ============


@pytest.mark.asyncio
async def test_stream_iteration_limit_yields_degradation():
    """触及迭代上限 → yield 降级提示，hit_iteration_limit=True。"""
    tool = _make_tool("get_time", reply="12:00")
    # 每轮都返回 tool_call（模拟死循环），由迭代上限拦下。
    rounds = [[_tc_chunk("get_time", call_id="c1")]]
    mock, _ = _make_stream_mock_llm(rounds)

    stream = await run_tool_loop_stream(
        chat_model=mock,
        tools=[tool],
        messages=[HumanMessage(content="无限调工具")],
        max_iterations=2,
    )
    chunks, final = await _collect(stream)

    yielded = "".join(chunks)
    assert "上限" in yielded
    assert final is not None
    assert final.hit_iteration_limit is True
    assert "上限" in final.answer
    # 触限前的工具调用仍记入审计。
    assert len(final.tool_calls_made) >= 1


@pytest.mark.asyncio
async def test_sync_tool_does_not_block_loop_and_receives_governance_context():
    """同步工具经标准 ainvoke 执行器运行，同时保留能力与审批 ContextVar。"""
    import asyncio
    import threading

    from bycall.governance.capability import CapabilitySet
    from bycall.mcp.context import (
        get_current_capability,
        reset_current_capability,
        set_current_capability,
    )
    from bycall.tools.approval import (
        ApprovalContext,
        get_approval_context,
        reset_approval_context,
        set_approval_context,
    )

    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    cap = CapabilitySet(user_id=1, user_name="alice", role="member")
    approval = ApprovalContext(user_id=1, user_name="alice", conversation_id=2, channel="api")
    observed = []

    def slow_tool() -> str:
        observed.append((threading.get_ident(), get_current_capability(), get_approval_context()))
        entered.set()
        assert release.wait(3)
        return "done"

    tool = StructuredTool.from_function(name="slow", description="慢工具", func=slow_tool)
    model, _ = _make_stream_mock_llm([[_tc_chunk("slow")], _text_chunks("answer")])
    cap_token = set_current_capability(cap)
    approval_token = set_approval_context(approval)
    stream = await run_tool_loop_stream(chat_model=model, tools=[tool], messages=[])
    task = asyncio.create_task(_collect(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
        assert observed == [(observed[0][0], cap, approval)]
        assert observed[0][0] != main_thread
        release.set()
        _, final = await asyncio.wait_for(task, 3)
        assert final.answer == "answer"
        assert final.tool_calls_made[0]["result"] == "done"
    finally:
        release.set()
        await task
        reset_approval_context(approval_token)
        reset_current_capability(cap_token)
