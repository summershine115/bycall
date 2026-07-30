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
from dataclasses import asdict
from datetime import UTC, datetime

from sqlmodel import Session

from bycall.agent.events import TaskEvent, TaskEventBus, default_bus
from bycall.governance.models import Run, Step, Task, User

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
    from bycall.agent.runtime_context import build_runtime_context
    from bycall.agent.session_store import load_user_memory
    from bycall.governance.capability import reload_user_and_compute_cap
    from bycall.mcp.context import reset_current_capability, set_current_capability
    from bycall.rag.retrieve import build_rag_messages
    from bycall.tools.approval import (
        ApprovalContext,
        reset_approval_context,
        set_approval_context,
    )
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

    task = session.get(Task, step.task_id)
    if task is None:
        raise ValueError(f"Step #{step.id} 关联的 Task #{step.task_id} 不存在")
    snapshot = asdict(cap)
    for field in ("system_prompt", "skill_markdown", "knowledge_scope_text"):
        snapshot.pop(field)
    task.capability_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    session.add(task)
    session.commit()

    # 2. filter tools（脱敏包装在 to_langchain_tools 内部按 cap 闭包注入）。
    allowed_specs = filter_tools(cap.allowed_tools)
    langchain_tools = to_langchain_tools(allowed_specs, cap)
    user_memories = load_user_memory(session, user.id) if user.id is not None else []
    runtime_context = build_runtime_context(
        user=user,
        capability=cap,
        channel=task.channel,
        conversation_id=task.conversation_id,
        tools=allowed_specs,
        has_history=False,
        has_summary=False,
        memory_count=len(user_memories),
    )

    # 3. 拼 messages（system + skill + knowledge + step goal；S1a 无独立 RAG，知识靠工具）。
    raw_messages = build_rag_messages(
        cap.system_prompt,
        goal,
        [],  # S1a：Step 不跑独立 RAG（多步任务的 RAG 在对话触发层做）
        skill_markdown=cap.skill_markdown,
        knowledge_scope_text=cap.knowledge_scope_text,
        runtime_context=runtime_context,
        user_memories=user_memories,
    )
    messages = _to_langchain_messages(raw_messages)

    # 4. 注入 capability 与审批上下文 → run_tool_loop → reset。
    cap_token = set_current_capability(cap)
    approval_token = set_approval_context(
        ApprovalContext(
            user_id=user.id,  # type: ignore[arg-type]
            user_name=user.name,
            conversation_id=task.conversation_id,
            channel=task.channel,
        )
    )
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
        reset_approval_context(approval_token)

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


# ============ 任务状态机（spec §3.2）============


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"planning", "cancelled"},
    "planning": {"running", "failed", "cancelled"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "cancelled"},
    "completed": set(),  # 终态
    "failed": set(),  # 终态
    "cancelled": set(),  # 终态
}
"""Task 合法状态流转（spec §3.2）。终态不可转。"""

_SUPPORTED_STEP_KINDS = {"tool_loop", "approval_wait", "clarify"}


def _fail_task(session: Session, task: Task, message: str) -> None:
    """把可执行性错误持久化为 failed，再把原因交给调用方处理。"""
    transition_task_status(session, task, "failed")
    raise ValueError(message)


def _require_steps(session: Session, task: Task, steps: list[Step]) -> None:
    """任务必须至少包含一个可执行 Step，禁止零步误报完成。"""
    if not steps:
        _fail_task(session, task, f"任务 #{task.id} 没有可执行步骤")


def _validate_step_kinds(session: Session, task: Task, steps: list[Step]) -> None:
    """执行前校验整份计划，避免前置步骤产生副作用后才发现后置 kind 不支持。"""
    unsupported = next((step for step in steps if step.kind not in _SUPPORTED_STEP_KINDS), None)
    if unsupported is None:
        return
    unsupported.status = "failed"
    session.add(unsupported)
    session.commit()
    _fail_task(session, task, f"不支持的 Step kind: {unsupported.kind}")


def transition_task_status(session: Session, task: Task, new_status: str) -> None:
    """校验并应用 Task 状态流转；非法流转抛 ``ValueError``。

    状态机见 spec §3.2。幂等流转（如已 running 再转 running）也视为非法——
    调用方应先判当前状态，避免重复触发副作用。
    """
    allowed = _ALLOWED_TRANSITIONS.get(task.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"非法任务状态流转：{task.status} → {new_status}"
            f"（允许：{sorted(allowed) or '终态不可转'}）"
        )
    task.status = new_status
    session.add(task)
    session.commit()


def create_steps_from_plan(session: Session, task: Task, plan: list[dict]) -> list[Step]:
    """从计划创建 Step，并与 ``task.plan_json`` 在同一事务持久化。

    幂等：Task 已有 Steps（resume）则直接返回，不重复创建。
    """
    existing = list(session.exec(Step.select_by_task(task.id)))
    if existing:
        return existing

    steps: list[Step] = []
    for i, item in enumerate(plan):
        step = Step(
            task_id=task.id,
            step_index=i,
            kind=item.get("kind", "tool_loop"),
            spec_json=json.dumps({"goal": item.get("goal", "")}, ensure_ascii=False),
            idempotency_key=f"task-{task.id}-step-{i}",
        )
        session.add(step)
        steps.append(step)
    task.plan_json = json.dumps(plan, ensure_ascii=False)
    session.add(task)
    session.commit()
    for step in steps:
        session.refresh(step)
    return steps


def _step_goal(step: Step) -> str:
    """从 step.spec_json 提取目标（``{"goal": "..."}`` 或纯文本兜底）。"""
    try:
        spec = json.loads(step.spec_json)
        if isinstance(spec, dict) and "goal" in spec:
            return str(spec["goal"])
    except (json.JSONDecodeError, TypeError):
        pass
    return step.spec_json


# ============ 任务编排引擎 ============


def run_task(
    *,
    session: Session,
    task: Task,
    chat_model,
    plan: list[dict] | None = None,
    bus: TaskEventBus = default_bus,
) -> Task:
    """编排执行一个 Task：状态机驱动，逐步执行 Steps（支持 resume）。

    - ``draft``：完整流程——发 task_created / plan_produced，按 plan 创建 Steps，
      planning→running→逐步执行。
    - ``paused`` / ``running``：resume——加载已有 Steps，``done`` 跳过（幂等），
      从第一个未完成处继续。
    - 终态（completed/failed/cancelled）：抛 ``ValueError``。

    逐步执行：``tool_loop`` 调 Step 执行器（失败 → task failed）；``approval_wait`` /
    ``clarify`` → ``paused`` 挂起；全 done → ``completed``。

    ``plan`` 仅首次（draft 且无 Steps）生效；resume 传 None。
    """
    if task.status == "draft":
        bus.publish(TaskEvent(type="task_created", task_id=task.id, payload={"goal": task.goal}))
        transition_task_status(session, task, "planning")
        steps = list(session.exec(Step.select_by_task(task.id)))
        if not steps:
            if not plan:
                _fail_task(session, task, f"任务 #{task.id} 没有可执行步骤")
            steps = create_steps_from_plan(session, task, plan)
        _require_steps(session, task, steps)
        bus.publish(
            TaskEvent(
                type="plan_produced",
                task_id=task.id,
                payload={"steps": len(steps), "kinds": [s.kind for s in steps]},
            )
        )
        transition_task_status(session, task, "running")
    elif task.status == "paused":
        transition_task_status(session, task, "running")
        steps = list(session.exec(Step.select_by_task(task.id)))
    elif task.status == "running":
        steps = list(session.exec(Step.select_by_task(task.id)))
    else:
        raise ValueError(f"任务不可执行（当前状态 {task.status}）")

    _require_steps(session, task, steps)
    _validate_step_kinds(session, task, steps)

    user = session.get(User, task.user_id)

    for step in steps:
        if step.status == "done":
            continue  # 幂等：跳过已完成

        if step.kind == "tool_loop":
            execute_tool_loop_step(
                session=session,
                user=user,
                step=step,
                goal=_step_goal(step),
                chat_model=chat_model,
                bus=bus,
            )
            if step.status == "failed":
                transition_task_status(session, task, "failed")
                return task
            # 继续下一个步骤。
        elif step.kind in ("approval_wait", "clarify"):
            # S1a 编排层只负责暂停并发事件；ApprovalRequest 创建/回填由后续切片接入。
            transition_task_status(session, task, "paused")
            event_type = "approval_requested" if step.kind == "approval_wait" else "task_paused"
            bus.publish(
                TaskEvent(
                    type=event_type,
                    task_id=task.id,
                    step_id=step.id,
                    payload={"reason": step.kind},
                )
            )
            return task
        else:  # pragma: no cover - 整份计划已在循环前校验
            raise AssertionError(f"未校验的 Step kind: {step.kind}")

    transition_task_status(session, task, "completed")
    bus.publish(TaskEvent(type="task_done", task_id=task.id, payload={"status": "completed"}))
    return task


def resume_pending_tasks(
    *,
    session: Session,
    chat_model,
    bus: TaskEventBus = default_bus,
) -> list[Task]:
    """恢复所有 paused / running 的未完成 Task（进程重启 / IM 断线后续跑）。

    扫描 ``status IN (paused, running)`` 的 Task，逐个调 ``run_task`` resume。
    ``run_task`` 内部幂等（done Step 跳过），故 running（中断）也能安全续跑。

    用于应用启动 / 渠道重连后扫库恢复未完成任务（spec §3.4）。单个 Task 续跑失败
    不影响其他（记日志，继续）。
    """
    pending: list[Task] = []
    for status in ("paused", "running"):
        pending.extend(session.exec(Task.select_by_status(status)))

    resumed: list[Task] = []
    for task in pending:
        try:
            run_task(session=session, task=task, chat_model=chat_model, bus=bus)
            resumed.append(task)
        except Exception as e:
            logger.error("Task #%s 续跑失败: %s", task.id, e)
    return resumed
