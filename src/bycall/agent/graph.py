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
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
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

    手动 astream 工具回路（不再用 ``graph.invoke`` + 重跑 ``astream``），保证：
    - 最终答案只 astream 一次（无浪费的二次 LLM 调用、无两次生成答案不一致）。
    - 工具调用回合也走流式（边流边判断回合类型），但文本被抑制、不外送——避免污染
      ``run_agent_stream`` 累积进 DB/审计的 ``full_answer``。
    - 最后 yield 一个 ``(None, True, GraphResult)`` 标记结束并携带审计数据。

    回合类型判定（OpenAI 兼容流式约定）：用第一个有意义的 chunk。
    - 带 ``tool_call_chunks``（content 空）→ 工具回合：累加 chunk、执行工具、不 yield 文本。
    - 带 content（无 tool_call_chunks）→ 最终答案回合：逐 token yield。

    AIMessageChunk 累加（``acc = acc + chunk``）：``acc.tool_calls`` 给出组装好的完整
    tool_calls，``acc.content`` 给完整 content。工具执行复用 ``ToolNode``（与同步
    ``run_tool_loop`` 一致），审计记录复用 ``_collect_tool_calls``（含 result 截断、
    「请求未执行」兜底）。

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
                content = _content_to_text(getattr(chunk, "content", None))
                if content:
                    full_content += content
                    yield content, False, None
            yield None, True, GraphResult(answer=full_content)
            return

        # 有工具：手动 astream 回路（model → tool → model，直到无 tool_calls）。
        model_with_tools = chat_model.bind_tools(tools)
        tool_by_name = {t.name: t for t in tools}
        current_messages: list[BaseMessage] = list(messages)

        for _iteration in range(max_iterations):
            # 每轮 astream：边流边累加 chunk、判断回合类型。
            acc: AIMessageChunk | None = None
            round_text = ""
            # None=未定, "tool"=工具回合, "answer"=最终答案回合。
            round_kind: str | None = None

            async for chunk in model_with_tools.astream(current_messages):
                acc = chunk if acc is None else acc + chunk
                content = _content_to_text(getattr(chunk, "content", None))
                has_tc = bool(getattr(chunk, "tool_call_chunks", None))
                # 用第一个有意义的 chunk 判定回合类型（tool_call_chunks 优先）。
                if round_kind is None and (has_tc or content):
                    round_kind = "tool" if has_tc else "answer"
                # 仅最终答案回合的文本外送（工具回合文本会污染 runner 累积的 answer）。
                if round_kind == "answer" and content:
                    round_text += content
                    yield content, False, None

            assembled_tool_calls = (acc.tool_calls if acc else []) or []
            # round_kind 判定为 answer 时即便偶发携带 tool_calls 也视作最终答案；
            # 仅当判定为工具回合、或完全未定型却仍累出 tool_calls 时才走工具分支。
            is_tool_round = round_kind == "tool" or (
                round_kind is None and bool(assembled_tool_calls)
            )

            if is_tool_round:
                # 工具回合：把组装好的 AIMessage（含 tool_calls）追加进上下文，
                # 然后逐个执行 tool_call。此处不能复用 ToolNode——它脱离 graph
                # 执行上下文时要求 LangGraph config（standalone invoke 会报缺
                # config key），故手动 tool.invoke 构造 ToolMessage，逻辑等价。
                # 工具回合的文本一律不向用户 yield（避免污染 runner 的 full_answer）。
                ai_msg = AIMessage(
                    content=_content_to_text(acc.content) if acc else "",
                    tool_calls=assembled_tool_calls,
                )
                current_messages.append(ai_msg)
                for tc in assembled_tool_calls:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("name", "")
                    tool = tool_by_name.get(tc_name)
                    if tool is None:
                        result_str = f"<unknown_tool: {tc_name}>"
                    else:
                        try:
                            result = tool.invoke(tc.get("args", {}))
                        except Exception as e:  # 工具执行异常不中断回路，记进审计。
                            logger.warning("工具 %s 执行失败: %s", tc_name, e)
                            result_str = f"<tool_error: {e}>"
                        else:
                            result_str = result if isinstance(result, str) else str(result)
                    current_messages.append(
                        ToolMessage(content=result_str, tool_call_id=tc_id, name=tc_name)
                    )
                continue

            # 最终答案回合：round_text 已逐 token yield；审计日志从完整消息序列重建
            # （复用 _collect_tool_calls：name/args/result 三元组，result 截断 + 未执行兜底）。
            yield (
                None,
                True,
                GraphResult(
                    answer=round_text,
                    tool_calls_made=_collect_tool_calls(current_messages),
                ),
            )
            return

        # 循环结束 = 触及迭代上限（LLM 在 max_iterations 轮里始终要求调工具）。
        logger.warning("流式工具回路触及迭代上限 %d，强制终止", max_iterations)
        yield _LIMIT_MESSAGE, False, None
        yield (
            None,
            True,
            GraphResult(
                answer=_LIMIT_MESSAGE,
                tool_calls_made=_collect_tool_calls(current_messages),
                hit_iteration_limit=True,
            ),
        )

    return _stream()


__all__ = ["GraphResult", "run_tool_loop", "run_tool_loop_stream"]
