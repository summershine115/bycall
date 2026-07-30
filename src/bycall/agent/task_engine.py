"""任务执行引擎（S1a 执行核心）。

Step 执行器：run_agent 的工具回路（``run_tool_loop``）作为 Step 内核——每个 Step 是一次
目标驱动的治理链路执行（重算能力集 → filter tools → 拼 messages → run_tool_loop → 产 Run）。
任务状态机 + 多 Step 编排见 T4（同文件后续补充）。

设计要点：
- **不重写工具回路**：Step 执行器复用 ``graph.run_tool_loop``，外层只做编排 + 落库 + 事件。
- **每步重算能力集**：复用 ``execute_approved_tool`` 同款 ``reload_user_and_compute_cap``，
  等待后执行用最新用户状态（红线 #7——陈旧权限不放行）。
- **脱敏零额外成本**：``to_langchain_tools`` 内部已按 cap 包装 redactor，Step 执行器不重复脱敏。
- **S1a 简化**：Step 不跑独立 RAG（知识靠工具获取）；多步任务的 RAG 在对话触发层做。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from sqlmodel import Session

from bycall.agent.events import TaskEvent, TaskEventBus, default_bus
from bycall.governance.models import Run, Step, User

logger = logging.getLogger(__name__)


def execute_tool_loop_step(
    *,
    session: Session,
    user: User,
    step: Step,
    goal: str,
    chat_model,
    bus: TaskEventBus = default_bus,
) -> Run:
    """执行一个 ``kind=tool_loop`` 的 Step，返回落库的 Run。

    复用 run_agent 的治理链路（不重写工具回路）：
    1. 重算能力集（执行前再验，权限可能已变更 → filter 出的工具集随之收紧）。
    2. filter tools（脱敏包装由 ``to_langchain_tools`` 内部完成）。
    3. 拼 messages（system + skill + knowledge + step goal；S1a 不跑独立 RAG，知识靠工具）。
    4. set_current_capability（MCP 鉴权）→ run_tool_loop → reset。
    5. 产 Run（input/output/tool_calls/duration）+ 发 step_started / step_completed 事件。

    失败语义：用户禁用 / 角色未定义（PermissionError）或 run_tool_loop 抛错 →
    ``Run.status="error"`` + 发 step_failed 事件、Step 标 failed，不向调用方抛。
    """
    from bycall.agent.graph import run_tool_loop
    from bycall.agent.runner import _to_langchain_messages
    from bycall.governance.capability import reload_user_and_compute_cap
    from bycall.mcp.context import reset_current_capability, set_current_capability
    from bycall.rag.retrieve import build_rag_messages
    from bycall.tools.gateway import filter_tools, to_langchain_tools

    started_at = datetime.now(UTC)
    t0 = time.perf_counter()

    # 标记 step running + 发事件。
    step.status = "running"
    session.add(step)
    session.commit()
    bus.publish(TaskEvent(type="step_started", task_id=step.task_id, step_id=step.id))

    run = Run(
        step_id=step.id,
        started_at=started_at,
        input_json=json.dumps({"goal": goal}, ensure_ascii=False),
    )

    # 1. 重算能力集（执行前用最新用户状态，权限可能已变更）。
    try:
        cap = reload_user_and_compute_cap(session, user.id)  # type: ignore[arg-type]
    except PermissionError as e:
        logger.warning("Step #%s 执行被拒（用户状态）: %s", step.id, e)
        run.status = "error"
        run.output_json = json.dumps({"error": str(e)}, ensure_ascii=False)
        _finish(session, step, run, step_status="failed", t0=t0)
        bus.publish(
            TaskEvent(
                type="step_failed",
                task_id=step.task_id,
                step_id=step.id,
                payload={"reason": "permission_denied", "error": str(e)},
            )
        )
        return run

    # 2. filter tools（脱敏包装在 to_langchain_tools 内部按 cap 闭包注入）。
    allowed_specs = filter_tools(cap.allowed_tools)
    langchain_tools = to_langchain_tools(allowed_specs, cap)

    # 3. 拼 messages（system + skill + knowledge + step goal；S1a 无独立 RAG，知识靠工具）。
    raw_messages = build_rag_messages(
        cap.system_prompt,
        goal,
        [],  # S1a：Step 不跑独立 RAG（多步任务的 RAG 在对话触发层做）
        skill_markdown=cap.skill_markdown,
        knowledge_scope_text=cap.knowledge_scope_text,
    )
    messages = _to_langchain_messages(raw_messages)

    # 4. set capability token → run_tool_loop → reset。
    cap_token = set_current_capability(cap)
    try:
        graph_result = run_tool_loop(
            chat_model=chat_model,
            tools=langchain_tools,
            messages=messages,
        )
    except Exception as e:
        logger.error("Step #%s run_tool_loop 失败: %s", step.id, e)
        run.status = "error"
        run.output_json = json.dumps({"error": str(e)}, ensure_ascii=False)
        _finish(session, step, run, step_status="failed", t0=t0)
        bus.publish(
            TaskEvent(
                type="step_failed",
                task_id=step.task_id,
                step_id=step.id,
                payload={"reason": "execution_error", "error": str(e)},
            )
        )
        return run
    finally:
        reset_current_capability(cap_token)

    # 5. 成功：产 Run（含脱敏后 tool_calls）+ 标 step done + 发事件。
    run.status = "ok"
    run.output_json = json.dumps(
        {"answer": graph_result.answer, "hit_iteration_limit": graph_result.hit_iteration_limit},
        ensure_ascii=False,
    )
    run.tool_calls_json = json.dumps(graph_result.tool_calls_made, ensure_ascii=False)
    _finish(session, step, run, step_status="done", t0=t0)
    bus.publish(
        TaskEvent(
            type="step_completed",
            task_id=step.task_id,
            step_id=step.id,
            payload={
                "answer_len": len(graph_result.answer),
                "tool_calls": len(graph_result.tool_calls_made),
            },
        )
    )
    return run


def _finish(session: Session, step: Step, run: Run, *, step_status: str, t0: float) -> None:
    """落库 Run + 更新 step.status，算 duration。"""
    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((time.perf_counter() - t0) * 1000)
    step.status = step_status
    session.add(run)
    session.add(step)
    session.commit()
    session.refresh(run)
