"""HITL 审批门——破坏性工具调用的人审拦截（M4 Slice C）。

product-design.md 支柱 3：破坏性操作（删数据 / 发外部邮件 / 大额操作）走 HITL 审批。
本模块是这条红线的落地。

拦截链路（在 ``tools/gateway.to_langchain_tools`` 中接线）：
  LLM 调 destructive 工具 → 审批门（本模块）
    → 创建 ``ApprovalRequest(pending)`` → 返回"待审批"消息给 LLM
    → LLM 告知用户"已提交审批，请等待"

执行链路（admin 审批通过时，在 ``admin/api.py`` 接线）：
  admin approve → ``execute_approved_tool``（本模块）
    → 重新计算能力集 + 验证工具仍在白名单
    → 设置 capability token（MCP server 鉴权）
    → 调 ``spec.func(**args)`` + 脱敏 → 结果存 ``ApprovalRequest`` + 追加会话

与 capability token（Slice B）的关系：审批门和 capability token 都在工具执行链路上，
但关注点不同——token 解决"调用时注入身份让 MCP server 自行鉴权"，审批门解决"破坏性
操作在执行前必须经过人审"。审批门拦截的是 func 层（返回 pending 而非执行），token
注入的是 meta 层（传给 MCP server）。两者正交，不冲突。

无 checkpointer 时的闭环设计（对齐 ADR-0002）：
graph 无状态执行，审批门在 func 层拦截——返回"待审批"消息后本轮对话正常结束（LLM
告知用户等待）。admin 审批通过后，``execute_approved_tool`` 用存储的参数实际执行工具，
结果追加到会话历史（``Message`` 表），用户下次交互时 agent 加载历史即可看到执行结果。
不需要 checkpointer / interrupt——会话状态由 session_store 管，与 ADR-0002 自洽。
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bycall.db import session_scope
from bycall.gateway.manager import get_channel_manager

if TYPE_CHECKING:
    from bycall.tools.base import ToolSpec

logger = logging.getLogger(__name__)

# 每个请求的审批上下文——runner 在调用 graph 前设置，审批门读取。
# 与 capability token 平行的 contextvars 机制（隔离保证相同：按 asyncio.Task/线程隔离）。
_current_approval_ctx: contextvars.ContextVar[ApprovalContext | None] = contextvars.ContextVar(
    "bycall_approval_ctx", default=None
)

# 审批结果文本截断长度（防爆库 / 防 token 膨胀）。
_RESULT_TRUNCATE = 2000


@dataclass(frozen=True)
class ApprovalContext:
    """审批上下文——工具执行时携带，供审批门创建审批请求。

    由 runner 在 graph 执行前设置（与 capability token 同一时机），审批门在拦截
    破坏性工具调用时读取。``conversation_id`` 可空（REST 单轮请求无会话）。
    ``task_id`` / ``step_id`` 可空——任务内审批时填写，供 approve/reject 后恢复任务
    （S1b 审批自动闭环）；对话内审批（无任务）时为空。
    """

    user_id: int
    user_name: str
    conversation_id: int | None
    channel: str
    im_user_id: str = ""
    task_id: int | None = None
    step_id: int | None = None


def set_approval_context(ctx: ApprovalContext | None) -> contextvars.Token:
    """设置当前请求的审批上下文（返回 token，需在 finally 中 reset）。"""
    return _current_approval_ctx.set(ctx)


def reset_approval_context(token: contextvars.Token) -> None:
    """恢复审批上下文到设置前的状态。"""
    _current_approval_ctx.reset(token)


def get_approval_context() -> ApprovalContext | None:
    """读取当前请求的审批上下文（审批门调用时用）。"""
    return _current_approval_ctx.get(None)


def create_approval_request(ctx: ApprovalContext, tool_name: str, args: dict[str, Any]) -> int:
    """创建一条 pending 审批请求，返回其 id。

    用独立 ``session_scope``（与 runner 的 session 隔离），立即提交——admin 队列需立即可见。
    任务内审批（``ctx.task_id`` 非空）把 Task/Step 关联写入，approve/reject 后据此恢复任务。
    """
    from bycall.governance.models import ApprovalRequest

    args_json = json.dumps(args, ensure_ascii=False, default=str)
    with session_scope() as session:
        req = ApprovalRequest(
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            conversation_id=ctx.conversation_id,
            channel=ctx.channel,
            im_user_id=ctx.im_user_id,
            task_id=ctx.task_id,
            step_id=ctx.step_id,
            tool_name=tool_name,
            tool_args_json=args_json,
            status="pending",
        )
        session.add(req)
        session.flush()  # 拿到自增 id
        assert req.id is not None  # flush 后自增主键必已填充
        req_id: int = req.id
    from bycall.observability.metrics import HUMAN_TAKEOVER

    HUMAN_TAKEOVER.labels(kind="approval_requested").inc()
    logger.info("审批请求 #%d 已创建: tool=%s user=%s", req_id, tool_name, ctx.user_name)
    return req_id


@dataclass(frozen=True)
class ActiveGrant:
    """命中的 active 授权快照（值对象）——session 关闭后 ORM 实例即 detached，
    在 session 内提取成纯值再返回，调用方安全访问。"""

    id: int
    scope: str
    granted_by: str


def find_active_grant(
    user_id: int, tool_name: str, conversation_id: int | None
) -> ActiveGrant | None:
    """查命中当前调用的 active 授权，无命中返回 None（T3.3 三档判定）。

    判定规则（ADR-0011）：
    - ``always`` 档：同 user + 同工具即命中（与会话无关）。
    - ``session`` 档：还要求 ``conversation_id`` 与授权绑定的会话一致——跨会话不命中，
      防止 A 会话的授权串到 B 会话（红线 #2 会话隔离在审批授权上的投影）。
    - 用户不同 / 工具不同 / 已撤销：一律不命中（回退人审）。

    返回 ``ActiveGrant`` 值对象（非 ORM 实例，避免 detached 访问报错）。
    """
    from bycall.governance.models import ApprovalGrant

    hit = None
    with session_scope() as session:
        grants = session.exec(ApprovalGrant.select_active(user_id, tool_name)).all()
        for g in grants:
            if g.scope == "always":
                hit = g
                break
            if (
                g.scope == "session"
                and g.conversation_id is not None
                and conversation_id is not None
                and g.conversation_id == conversation_id
            ):
                hit = g
                break
        if hit is not None:
            return ActiveGrant(id=hit.id, scope=hit.scope, granted_by=hit.granted_by)
    return None


def _record_grant_execution(
    ctx: ApprovalContext, spec: ToolSpec, kwargs: dict[str, Any], grant, result_text: str
) -> None:
    """把授权免审执行写进审批台账（status=executed + grant_id）——免审 ≠ 免记录。

    与 ``create_approval_request`` 同样的独立 ``session_scope`` + 立即提交模式。
    ``reviewed_by`` 记 ``grant:{scope}#{id}``，admin 审批队列里可区分人审与授权执行。
    """
    from datetime import UTC, datetime

    from bycall.governance.models import ApprovalRequest
    from bycall.observability.metrics import APPROVAL_GRANT_BYPASS

    args_json = json.dumps(kwargs, ensure_ascii=False, default=str)
    now = datetime.now(UTC)
    with session_scope() as session:
        req = ApprovalRequest(
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            conversation_id=ctx.conversation_id,
            channel=ctx.channel,
            im_user_id=ctx.im_user_id,
            task_id=ctx.task_id,
            step_id=ctx.step_id,
            tool_name=spec.name,
            tool_args_json=args_json,
            status="executed",
            reviewed_by=f"grant:{grant.scope}#{grant.id}",
            grant_id=grant.id,
            executed_at=now,
            review_note=f"授权免审执行（{grant.scope} 档，由 {grant.granted_by} 授权）",
            result_text=result_text[:_RESULT_TRUNCATE],
        )
        session.add(req)
    APPROVAL_GRANT_BYPASS.labels(scope=grant.scope).inc()
    logger.info(
        "授权免审执行: tool=%s user=%s grant=%s#%s",
        spec.name,
        ctx.user_name,
        grant.scope,
        grant.id,
    )


def make_approval_gated_func(spec: ToolSpec, cap=None):
    """构造审批门包装的 func：破坏性工具默认创建审批请求，命中授权时免审直执行。

    判定顺序（T3.3 三档）：
    1. 无审批上下文（admin 连通性测试 / 单测直接调 func）→ 降级直接执行（既有语义）。
    2. 命中 active 授权（always / session 且会话匹配）→ **当轮直接执行** + 脱敏 +
       台账记录（``grant_id``）——跳过的只是 HITL 人审，工具可见性仍由权限矩阵在
       graph 构建时决定（LLM 看不到的工具进不了这里，授权无法越权）。
    3. 未命中 → 创建 pending 审批请求 + 返回"待审批"消息（既有语义）。

    ``cap`` 用于免审执行路径的统一脱敏管道（与正常工具一致）；为 None 时跳过脱敏
    （仅出现在不经过 ``to_langchain_tools`` 的直连测试场景）。
    ``StructuredTool`` 按 schema 反射调用（``**kwargs``），故签名用 ``**kwargs``。
    """

    def _gated(**kwargs: Any) -> str:
        ctx = get_approval_context()
        if ctx is None:
            # 无审批上下文（admin 测试连通性 / 单元测试直接调 func）→ 降级直接执行。
            return spec.func(**kwargs)
        grant = find_active_grant(ctx.user_id, spec.name, ctx.conversation_id)
        if grant is not None:
            result = spec.func(**kwargs)
            if cap is not None:
                from bycall.tools.redactor import sanitize_tool_result

                result = sanitize_tool_result(result, cap, redactor=spec.redactor)
            result_text = str(result)
            _record_grant_execution(ctx, spec, kwargs, grant, result_text)
            return result_text
        req_id = create_approval_request(ctx, spec.name, kwargs)
        args_summary = _summarize_args(kwargs)
        return (
            f"⚠️ 该操作需要管理员审批。已提交审批请求 #{req_id}"
            f"（工具：{spec.name}"
            + (f"，参数：{args_summary}" if args_summary else "")
            + "）。审批通过后将自动执行并记录结果。请告知用户等待审批。"
        )

    return _gated


def execute_approved_tool(req) -> tuple[bool, str]:
    """执行已批准的工具调用，返回 ``(是否成功, 结果文本)``。

    审批通过后的执行路径（admin approve 端点调用）：
    1. 从注册表查工具 spec（可能已被禁用/删除 → 失败）。
    2. 重新计算用户能力集（权限可能已变更 → 若工具不再在白名单 → 拒绝）。
    3. 设置 capability token（MCP server 鉴权用，与正常调用一致）。
    4. 调 ``spec.func(**args)`` + 脱敏（若有 redactor）。
    5. 返回脱敏后结果（截断到 ``_RESULT_TRUNCATE``）。

    ``req`` 是 ``ApprovalRequest`` ORM 实例（status 应为 pending，由调用方保证）。

    S1b 任务级审批（``__task_approval__`` 伪工具）：approval_wait Step 的审批是纯 HITL
    确认（无实际工具执行），直接通过——真实工具的审批仍走下方完整链路。
    """
    from bycall.governance.capability import reload_user_and_compute_cap
    from bycall.mcp.context import reset_current_capability, set_current_capability
    from bycall.tools.gateway import filter_tools
    from bycall.tools.redactor import sanitize_tool_result
    from bycall.tools.registry import get_tool

    # 任务级审批（approval_wait Step）：管理员确认即通过，无工具可执行。
    if req.tool_name == "__task_approval__":
        logger.info("审批 #%s 任务级确认通过", req.id)
        return True, "任务审批已通过"

    spec = get_tool(req.tool_name)
    if spec is None:
        msg = f"工具 {req.tool_name} 不再可用（可能已被禁用或移除），无法执行"
        logger.warning("审批 #%s 执行失败: %s", req.id, msg)
        return False, msg

    args = json.loads(req.tool_args_json) if req.tool_args_json else {}

    # 重新计算能力集（重新加载用户最新状态——审批等待期间权限可能已变更）。
    # 与 Step 执行器共用 reload_user_and_compute_cap，避免陈旧权限放行（红线 #7）。
    with session_scope() as session:
        try:
            cap = reload_user_and_compute_cap(session, req.user_id)
        except PermissionError as e:
            logger.warning("审批 #%s 执行被拒（用户状态）: %s", req.id, e)
            return False, str(e)
        # 过滤验证：工具是否仍在用户当前白名单内。
        allowed_names = {s.name for s in filter_tools(cap.allowed_tools)}

    if spec.name not in allowed_names:
        msg = f"工具 {spec.name} 已不在用户当前权限范围内，执行被拒绝"
        logger.warning("审批 #%s 执行被拒: %s", req.id, msg)
        return False, msg

    # 设置 capability token（MCP server 鉴权用）。
    cap_token = set_current_capability(cap)
    try:
        result = spec.func(**args)
        # 统一脱敏 + 截断（缝隙 #5）：与网关同管道，审批执行的结果也受大小限制保护。
        result_str = sanitize_tool_result(result, cap, redactor=spec.redactor)
    except Exception as e:
        logger.warning("审批 #%s 工具 %s 执行异常: %s", req.id, spec.name, e)
        return False, f"工具执行异常: {e}"
    finally:
        reset_current_capability(cap_token)

    logger.info("审批 #%s 执行成功: tool=%s result_len=%d", req.id, spec.name, len(result_str))
    return True, result_str


def _summarize_args(args: dict[str, Any], max_len: int = 200) -> str:
    """把工具参数摘要成短字符串（给 LLM 看的"待审批"消息用，避免过长）。"""
    if not args:
        return ""
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return s[:max_len] + ("…" if len(s) > max_len else "")


# M6 主动推送结果正文的截断长度（企微单条 markdown 不宜过长）。
_PUSH_RESULT_TRUNCATE = 800


def push_approval_result(req, result_text: str) -> None:
    """提交审批结果后，向当前仍获授权的企微身份尽力推送。

    通知是已提交状态的附加效果，不能影响审批事务。发送前重新验证用户仍启用且企微绑定
    未变化，避免审批等待期间身份转移后把敏感结果推给错误账号。
    """
    if getattr(req, "channel", "") != "wecom":
        logger.debug(
            "审批 #%s 推送跳过：非企微渠道 channel=%s", req.id, getattr(req, "channel", "")
        )
        return

    im_user_id = _resolve_current_im_user_id(req)
    if not im_user_id:
        return

    body = {
        "msgtype": "markdown",
        "markdown": {"content": _build_approval_push_markdown(req, result_text)},
    }
    try:
        ok = get_channel_manager().send_proactive(im_user_id, body)
    except Exception:
        logger.exception("审批 #%s 主动推送异常（已吞掉，不影响审批结果）", req.id)
        return
    if not ok:
        logger.warning("审批 #%s 主动推送未送达（渠道未运行/未连接）", req.id)


def _resolve_current_im_user_id(req) -> str:
    """返回当前仍属于请求用户的有效企微身份；授权变化时返回空串。"""
    from sqlmodel import select

    from bycall.governance.models import IMBinding, User

    try:
        with session_scope() as session:
            user = session.get(User, req.user_id)
            if user is None or not user.is_active:
                logger.debug("审批 #%s 推送跳过：用户不存在或已禁用", req.id)
                return ""
            binding = session.exec(
                select(IMBinding).where(
                    IMBinding.channel == "wecom",
                    IMBinding.user_id == req.user_id,
                )
            ).first()
            current_id = getattr(binding, "im_user_id", "") or ""
    except Exception:
        logger.exception("反查当前企微身份异常: user_id=%s", req.user_id)
        return ""

    captured_id = getattr(req, "im_user_id", "") or ""
    if not current_id:
        logger.debug("审批 #%s 推送跳过：当前无企微绑定", req.id)
        return ""
    if captured_id and captured_id != current_id:
        logger.warning("审批 #%s 推送跳过：发起身份与当前绑定不一致", req.id)
        return ""
    return current_id


def _build_approval_push_markdown(req, result_text: str) -> str:
    """构造审批结果推送的企微 markdown 正文。"""
    text = result_text or ""
    snippet = text[:_PUSH_RESULT_TRUNCATE]
    suffix = "…" if len(text) > _PUSH_RESULT_TRUNCATE else ""
    req_id = getattr(req, "id", "?")
    tool_name = getattr(req, "tool_name", "?")
    return (
        f"✅ **审批 #{req_id} 已通过并执行**\n\n"
        f"**工具**：{tool_name}\n\n"
        f"**执行结果**：\n{snippet}{suffix}"
    )


__all__ = [
    "ActiveGrant",
    "ApprovalContext",
    "create_approval_request",
    "execute_approved_tool",
    "find_active_grant",
    "get_approval_context",
    "make_approval_gated_func",
    "push_approval_result",
    "reset_approval_context",
    "set_approval_context",
]
