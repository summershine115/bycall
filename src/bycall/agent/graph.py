"""LangGraph 工具回路（ReAct）：model → tool → model，直到无 tool_calls。

M3 工具治理的执行核心（见 ADR-0002 渐进式引入）。把治理脊柱过滤后的工具集
（``gateway.filter_tools`` → ``gateway.to_langchain_tools``）bind 给 ChatOpenAI，
LLM 只见白名单内的工具——无权工具 LLM 根本看不到（支柱 3）。

回路结构（标准 LangGraph ReAct）：
  agent node（``chat_model.bind_tools(tools).invoke(messages)``）
    → ``tools_condition`` 路由（末条 AIMessage 有 tool_calls？）
      → 有：tool node（``ToolNode`` 并行执行 tool_calls）→ 回 agent node
      → 无：终止（END）

关键设计：
- **无状态执行**：graph 不挂 Checkpointer（见 ADR-0002 三大障碍）。历史由
  ``session_store`` 加载、拼进 initial messages；graph 跑完取末条 AIMessage.content
  作 answer 写回 DB。这保留了控制面会话端点的 SQL 数据源 + 审计语义。
- **迭代上限保护**（防 LLM 陷入"调工具→再调工具"死循环）：
  ``agent_node`` 闭包计数 ``iter_counter``，第 ``max_iterations+1`` 次进入时不再调
  LLM、直接返回降级 AIMessage；``route`` 见计数超限即返回 END 终止。两者都在单次
  ``graph.invoke`` 内的同一闭包上工作（LangGraph 顺序执行节点，闭包可靠）。
  ``recursion_limit = max_iterations * 2 + 3`` 仅作**硬性安全网**（防闭包逻辑意外失效），
  正常路径不依赖它、也不会触发 ``GraphRecursionError``。
- **空工具集退化**：``tools`` 为空时 graph 退化为单次 LLM 调用（纯对话 + RAG），
  行为与 M2 一致。

参考：LangGraph ``create_react_agent`` 的内部结构（agent + tools_condition + ToolNode），
手动用 StateGraph 组装以保留注入治理逻辑与工具调用审计的空间。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

logger = logging.getLogger(__name__)

# 迭代上限：一次对话最多允许的工具调用轮次。防 LLM 陷入死循环（费用/性能风险）。
_DEFAULT_MAX_ITERATIONS = 5

# 迭代上限触达时的降级提示。
_LIMIT_MESSAGE = "已达到工具调用次数上限，无法继续处理。请缩小问题范围后重试。"

# 审计记录的工具返回值（脱敏后）单条截断长度，防爆库/防 token 膨胀。
# 工具返回值已脱敏，但仍可能很长（如大表查询的截断结果），审计侧再截一道。
_RESULT_TRUNCATE = 1000


@dataclass
class GraphResult:
    """graph 执行结果。

    - ``answer``：最终回复文本（末条 AIMessage.content）。
    - ``tool_calls_made``：本次对话实际触发的工具调用记录（name, args），供审计。
    - ``hit_iteration_limit``：是否触及迭代上限（用于审计/降级提示）。
    """

    answer: str
    tool_calls_made: list[dict] = field(default_factory=list)
    hit_iteration_limit: bool = False


def run_tool_loop(
    *,
    chat_model: BaseChatModel,
    tools: list[BaseTool],
    messages: list[BaseMessage],
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
) -> GraphResult:
    """执行一次完整的工具回路。

    这是 ``run_agent`` 的"LLM 执行段"：拿到拼好的 messages + 过滤后的 tools，
    跑 model↔tool 循环直到 LLM 给出无 tool_calls 的最终回复。

    ``messages`` 用 LangChain 消息类型（SystemMessage/HumanMessage/AIMessage/ToolMessage），
    由 runner 在调用前从自研 ``LLMMessage`` 转换而来。

    返回 ``GraphResult``——answer + 工具调用记录（审计用）。
    """
    if not tools:
        # 无工具：退化单次调用（纯对话 + RAG），与 M2 行为一致。
        resp = chat_model.invoke(messages)
        return GraphResult(answer=_extract_text(resp))

    # 有工具：bind_tools 后走 graph 回路。
    model_with_tools = chat_model.bind_tools(tools)
    tool_node = ToolNode(tools)

    # 闭包跟踪 agent 调用计数（迭代上限）。工具调用记录（含返回值）改为末尾从
    # final_messages 统一重建——agent_node 只看 AIMessage 拿不到工具返回值（在
    # ToolMessage 里），末尾重建能一次拿到 name/args/result 三元组。
    iter_counter = {"n": 0}

    def agent_node(s: MessagesState) -> dict:
        """model 节点：调 LLM（bind_tools），返回新 AIMessage。"""
        iter_counter["n"] += 1
        # 迭代上限保护：第 max_iterations+1 次进入时不再调 LLM，给降级提示。
        if iter_counter["n"] > max_iterations:
            logger.warning("工具回路触及迭代上限 %d，强制终止", max_iterations)
            return {"messages": [AIMessage(content=_LIMIT_MESSAGE)]}
        response = model_with_tools.invoke(s["messages"])
        return {"messages": [response]}

    def route(s: MessagesState) -> str:
        """路由：已超迭代上限 → END；否则按 tools_condition 判末条消息。"""
        if iter_counter["n"] > max_iterations:
            return END
        return tools_condition(s)

    # 组装 graph：START → agent → (route) → tools → agent | END
    graph_builder = StateGraph(MessagesState)
    graph_builder.add_node("agent", agent_node)
    graph_builder.add_node("tools", tool_node)
    graph_builder.add_edge(START, "agent")
    graph_builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph_builder.add_edge("tools", "agent")
    graph = graph_builder.compile()

    # recursion_limit 作为硬性安全网（闭包 route 正常会先拦下，这里防意外）。
    recursion_limit = max_iterations * 2 + 3

    # 无 checkpointer：单次 invoke，状态不持久化（历史由 session_store 管）。
    result = graph.invoke({"messages": messages}, config={"recursion_limit": recursion_limit})
    final_messages: list[BaseMessage] = result["messages"]

    hit_limit = iter_counter["n"] > max_iterations
    return GraphResult(
        answer=_final_answer(final_messages),
        tool_calls_made=_collect_tool_calls(final_messages),
        hit_iteration_limit=hit_limit,
    )


def _content_to_text(content) -> str:
    """把 LangChain 消息 content 归一成 str。

    content 常规是 str，但也可能是内容块 list（OpenAI 兼容 / 推理模型——本项目
    经任意 ``base_url`` 明确支持这类 provider）。list 情形：拼接字符串项与 dict 块
    （``type == "text"``）的 ``text`` 字段；``None`` → ""；其余类型兜底 ``str()``。

    不归一会导致 ``answer`` 契约（``str``）被 list 破坏——``answer[:500]`` 切成 list、
    ``Message.content`` 存进非字符串、``.strip()`` 在 list 上抛 AttributeError。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def _final_answer(messages: list[BaseMessage]) -> str:
    """取最后一条 AIMessage.content 作最终回复。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return _content_to_text(msg.content)
    # 没有 AIMessage 的极端情况（不应发生），返回空串兜底。
    return ""


def _extract_text(response: BaseMessage) -> str:
    """从单次 LLM 响应提取文本（无工具路径用）。"""
    if isinstance(response, BaseMessage):
        return _content_to_text(response.content)
    return str(response)


def _collect_tool_calls(messages: list[BaseMessage]) -> list[dict]:
    """从 graph 执行后的消息序列重建工具调用记录（含脱敏后返回值）。

    工具返回值在 ``ToolMessage`` 里（``agent_node`` 只看 ``AIMessage`` 拿不到），
    故在 graph 跑完后统一重建：

    1. 第一遍扫所有 ``AIMessage``，按 ``tool_call_id`` 收集 LLM 请求的 ``(name, args)``
       （发起调用的 ``AIMessage.tool_calls`` 带 name + args，``ToolMessage`` 不带 args）。
    2. 第二遍扫所有 ``ToolMessage``，取 ``name`` + ``content``（经 gateway 脱敏包装
       后的实际返回值），关联 args，组装 ``[{name, args, result}]``。
    3. 第三遍兜底：``requested`` 里有但没有任何 ``ToolMessage`` 消费的 id，记为
       ``result="<not_executed>"`` ——这正是治理审计最该留痕的异常路径（LLM 请求了
       调用但因参数校验失败 / ToolNode 跳过等原因未执行）。不补则该调用从审计蒸发。

    ``result`` 截断到 ``_RESULT_TRUNCATE`` 防审计字段爆库。触及迭代上限时，之前轮次
    的 ``AIMessage`` / ``ToolMessage`` 仍在序列里，会被一并采集。
    """
    requested: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                requested[tc["id"]] = (tc.get("name", ""), tc.get("args", {}))

    calls: list[dict] = []
    executed_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage):
            executed_ids.add(msg.tool_call_id)
            content = msg.content
            if not isinstance(content, str):
                content = str(content)
            name, args = requested.get(msg.tool_call_id, (msg.name or "", {}))
            calls.append({"name": name, "args": args, "result": content[:_RESULT_TRUNCATE]})

    for tc_id, (name, args) in requested.items():
        if tc_id not in executed_ids:
            calls.append({"name": name, "args": args, "result": "<not_executed>"})

    return calls


async def run_tool_loop_stream(
    *,
    chat_model: BaseChatModel,
    tools: list[BaseTool],
    messages: list[BaseMessage],
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
):
    """流式执行工具回路——逐 token yield 最终答案。

    与 ``run_tool_loop`` 同构，但用 ``graph.astream_events`` 替代 ``graph.invoke``：
    - 工具调用回合（tool_call_chunks 非空）的 token 被抑制，不推送给用户。
    - 最终文本答案逐 token yield，用户实时看到生成过程。
    - 最后 yield 一个 ``(None, True, GraphResult)`` 标记结束并携带审计数据。

    Yields:
        ``(chunk_text: str | None, is_final: bool, result: GraphResult | None)``
        - 正常 token: ``("hello", False, None)``
        - 流结束: ``(None, True, GraphResult(...))``
    """

    async def _stream():
        if not tools:
            # 无工具：直接 astream，逐 token yield。
            full_content = ""
            async for chunk in chat_model.astream(messages):
                content = _content_to_text(chunk.content) if hasattr(chunk, "content") else ""
                if content:
                    full_content += content
                    yield content, False, None
            yield None, True, GraphResult(answer=full_content)
            return

        model_with_tools = chat_model.bind_tools(tools)
        tool_node = ToolNode(tools)
        iter_counter = {"n": 0}

        def agent_node(s: MessagesState) -> dict:
            iter_counter["n"] += 1
            if iter_counter["n"] > max_iterations:
                logger.warning("工具回路触及迭代上限 %d，强制终止", max_iterations)
                return {"messages": [AIMessage(content=_LIMIT_MESSAGE)]}
            response = model_with_tools.invoke(s["messages"])
            return {"messages": [response]}

        def route(s: MessagesState) -> str:
            if iter_counter["n"] > max_iterations:
                return END
            return tools_condition(s)

        graph_builder = StateGraph(MessagesState)
        graph_builder.add_node("agent", agent_node)
        graph_builder.add_node("tools", tool_node)
        graph_builder.add_edge(START, "agent")
        graph_builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
        graph_builder.add_edge("tools", "agent")
        graph = graph_builder.compile()

        recursion_limit = max_iterations * 2 + 3

        # 先用非流式 graph.invoke 处理工具调用回合（不向用户展示）。
        # 工具调用完成后，单独对最终消息列表做一次 astream 得到逐 token 流式输出。
        result = graph.invoke({"messages": messages}, config={"recursion_limit": recursion_limit})
        final_messages: list[BaseMessage] = result["messages"]
        hit_limit = iter_counter["n"] > max_iterations

        # 取最后一条 AIMessage 作为最终答案的起点——对其前面的消息做 astream。
        # 这样工具调用的 token 不会被用户看到，只看到最终答案的流式生成。
        answer_prefix = ""
        stream_input = list(messages)
        for msg in final_messages[len(messages) :]:
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                # 找到最终的纯文本 AIMessage：其前面是完整上下文，用它做 astream。
                answer_prefix = _content_to_text(msg.content)
                break
            stream_input.append(msg)

        if stream_input == list(messages):
            # 没有额外的 AIMessage（如纯 tool call 无文本回答）：直接用最终消息。
            graph_result = GraphResult(
                answer=answer_prefix or _final_answer(final_messages),
                tool_calls_made=_collect_tool_calls(final_messages),
                hit_iteration_limit=hit_limit,
            )
            if answer_prefix:
                yield answer_prefix, False, None
            yield None, True, graph_result
            return

        # 对 stream_input 做 astream——这会重新生成最终答案的每个 token。
        full_content = ""
        async for chunk in model_with_tools.astream(stream_input):
            content = _content_to_text(chunk.content) if hasattr(chunk, "content") else ""
            if content:
                full_content += content
                yield content, False, None

        # 用流式累积的 content 覆盖 graph.invoke 里那条 AIMessage（保证一致）。
        graph_result = GraphResult(
            answer=full_content or answer_prefix or _final_answer(final_messages),
            tool_calls_made=_collect_tool_calls(final_messages),
            hit_iteration_limit=hit_limit,
        )
        yield None, True, graph_result

    return _stream()


__all__ = ["GraphResult", "run_tool_loop", "run_tool_loop_stream"]


__all__ = ["GraphResult", "run_tool_loop", "run_tool_loop_stream"]
