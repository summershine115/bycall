"""Admin 控制 REST API：用户/角色/提示词/审计管理。

⚠️ 这些端点管理组织的治理资产，调用方必须是已认证的 admin。
M1 先用一个简单的 admin 守卫依赖（``_require_admin``）；正式会话认证由 auth 模块提供，
集成到 app 时替换为基于 session 的依赖。这里接口契约先定好。

错误约定：
- 资源不存在 → 404
- 冲突（重复唯一键 / 删除被引用的角色）→ 409
- 非法操作（删 admin 账号 / 角色名占保留字）→ 400
"""

# FastAPI 的依赖注入惯用法是 ``Depends(...)`` / ``Query(...)`` 作为参数默认值，
# 这是框架官方写法，与 ruff B008（不要在默认值里调函数）相冲突，整文件豁免。
# ruff: noqa: B008

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from bycall.admin.schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    ApprovalGrantOut,
    ApprovalOut,
    ApprovalReview,
    AuditOut,
    AuditPage,
    BatchReindexRequest,
    ChunkPreviewItem,
    ChunkPreviewRequest,
    ChunkPreviewResult,
    ConversationOut,
    DocumentContentOut,
    DocumentCreate,
    DocumentIngestResult,
    DocumentOut,
    DocumentUpdate,
    IngestAccepted,
    IngestProgressOut,
    KBChunkListResult,
    KBChunkOut,
    KBSearchRequest,
    KBSearchResult,
    KBSearchResultItem,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
    MCPServerCreate,
    MCPServerOut,
    MCPServerUpdate,
    MCPTestResult,
    MemoryCreate,
    MemoryOut,
    MemoryUpdate,
    MessageOut,
    ModelCreate,
    ModelOut,
    ModelTestResult,
    ModelUpdate,
    PolicySimulateRequest,
    PolicySimulateResult,
    PromptActivate,
    PromptCreate,
    PromptOut,
    PromptUpdate,
    ReleaseOut,
    ReleasePublishRequest,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    SettingOut,
    SettingsBatchUpdate,
    SkillCreate,
    SkillImportError,
    SkillImportItem,
    SkillImportResult,
    SkillOut,
    SkillUpdate,
    TaskDetailOut,
    TaskOut,
    TaskRunOut,
    TaskScheduleCreate,
    TaskScheduleOut,
    TaskScheduleUpdate,
    TaskStepOut,
    ToolOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from bycall.db import get_session, session_scope
from bycall.governance.models import (
    Agent,
    AuditLog,
    Conversation,
    Document,
    IMBinding,
    KnowledgeBase,
    Message,
    Model,
    ReleaseState,
    Role,
    Run,
    Skill,
    Step,
    SystemPrompt,
    SystemSetting,
    Task,
    TaskSchedule,
    User,
    UserMemory,
)
from bycall.governance.security import hash_password
from bycall.rag.ingest_tracker import ingest_tracker

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 知识库文档入库等控制面操作的日志（修复"入库失败无日志"——入库全链路在此留痕）。
logger = logging.getLogger(__name__)

# 内置保留角色名：不允许在 Role 表里建记录（policy 引擎对 "admin" 直接放全权）。
_RESERVED_ROLE_NAMES = frozenset({"admin"})

# 上传文件大小上限：10MB。超过 → 413，避免超大文件全量读入内存导致 OOM。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 审计日志中需要脱敏的字段名（值不落审计，只记 "<masked>"）。
_SENSITIVE_FIELDS = frozenset({"password_hash", "api_key", "value", "secret"})


# ============ 异步入库基础设施 ============
# 后台入库线程池（lazy singleton）。create/upload/reindex 提交后立即返回 IngestAccepted，
# 真正的 切块→向量化→写库 在此线程池里跑；HTTP 请求不阻塞。
# ⚠️ 线程内**必须**用独立的 ``session_scope()``，禁止跨线程共享请求的 Session
# （SQLAlchemy Session 非线程安全）。
_ingest_executor: ThreadPoolExecutor | None = None

# 已提交但未确认完成的后台入库 Future（测试用 ``_drain_ingest`` 排空，保证用例隔离）。
_ingest_futures: list = []

# 按 collection 维度的入库串行锁。zvec 的写锁靠 GC 释放（见 ZvecStore.close 注释），
# 同一 collection 并发 open+upsert 会互相踩写锁。这里按 collection_name 加锁，
# 保证同一 KB 的入库任务串行访问向量库，不同 KB 仍可并发——既守住 upsert/flush 契约
# （架构红线：不得在向量操作未确认前提交元数据），又保留跨知识库并发能力。
_collection_ingest_locks: dict[str, threading.Lock] = {}
_collection_ingest_locks_guard = threading.Lock()


def _get_collection_ingest_lock(collection_name: str) -> threading.Lock:
    """取（或懒建）某 collection 的入库串行锁。"""
    with _collection_ingest_locks_guard:
        lock = _collection_ingest_locks.get(collection_name)
        if lock is None:
            lock = threading.Lock()
            _collection_ingest_locks[collection_name] = lock
        return lock


def _get_ingest_executor() -> ThreadPoolExecutor:
    """后台入库线程池（lazy singleton，max_workers=4）。"""
    global _ingest_executor
    if _ingest_executor is None:
        _ingest_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")
    return _ingest_executor


def _audit_admin(
    session: Session,
    request: Request,
    action: str,
    entity: str = "",
    *,
    before: object = None,
    after: object = None,
) -> None:
    """写一条控制面审计日志。

    从 request cookie 解析操作者身份（user_id/user_name）。变更前后值序列化成
    JSON 字符串存 before/after 列；敏感字段（password_hash/api_key/secret）脱敏。

    不单独 commit——由调用方在业务 commit 时一起提交（与数据面审计一致）。
    """
    request.state.admin_audited = True

    from bycall.governance.auth import load_session_cookie

    operator_id: int | None = None
    operator_name = "unknown"
    token = request.cookies.get("bycall_session")
    if token:
        data = load_session_cookie(token)
        if data is not None:
            operator_id = data.user_id
            operator_name = data.user_name

    def _safe(obj: object) -> str:
        """序列化对象，敏感字段脱敏。"""
        if obj is None:
            return ""
        if isinstance(obj, str):
            return obj
        # SQLModel/SQLModel 实例 → dict
        if hasattr(obj, "model_dump"):
            d = obj.model_dump()
        elif isinstance(obj, dict):
            d = dict(obj)
        else:
            return str(obj)
        for k in list(d.keys()):
            if any(s in k.lower() for s in _SENSITIVE_FIELDS):
                d[k] = "<masked>"
        return json.dumps(d, ensure_ascii=False, default=str)

    session.add(
        AuditLog(
            user_id=operator_id,
            user_name=operator_name,
            channel="admin",
            action=action,
            question=entity,
            before=_safe(before),
            after=_safe(after),
        )
    )


def _require_admin() -> None:
    """占位 admin 守卫。集成阶段替换为基于 session 的真实认证。

    M1 先放行所有请求（仅 dev），生产前必须接真实认证。
    """
    # TODO(M1 集成): 替换为 require_admin_session dependency
    return None


@router.post("/policy/simulate", response_model=PolicySimulateResult)
def policy_simulate(
    payload: PolicySimulateRequest,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> PolicySimulateResult:
    """策略模拟器：给定用户 + 动作 + 资源，预演治理判定（allow/deny + 能力集 + 过滤条件）。

    让治理结果可独立验证——admin 在控制面就能看到「这个用户对检索/工具会得到什么判定、
    命中了哪些规则」，不靠读代码（战略 §4.1「策略模拟器」）。复用治理脊柱三个真实组件
    （compute_capability_set / build_acl_filters / filter_tools），与数据面实际判定同源，
    杜绝「模拟与真实不一致」。``agent_id`` 非空时按实例收窄后模拟——这是数字员工详情页
    「生效预览」的唯一数据源，避免前端自己推算能力边界。
    """
    from bycall.governance.capability import compute_capability_set
    from bycall.governance.models import Agent
    from bycall.governance.policy import build_acl_filters, subject_from_capability
    from bycall.tools.gateway import filter_tools

    user = session.get(User, payload.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"用户不存在: {payload.user_id}")

    agent: Agent | None = None
    if payload.agent_id is not None:
        agent = session.get(Agent, payload.agent_id)
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"数字员工不存在: {payload.agent_id}")
    agent_ctx = {"agent_id": payload.agent_id, "agent_name": agent.name if agent else ""}
    scope_label = f"实例 '{agent.name}'" if agent is not None else "角色"

    try:
        cap = compute_capability_set(session, user, agent)
    except PermissionError as e:
        return PolicySimulateResult(
            user_id=payload.user_id,
            user_name=user.name,
            role=user.role,
            level=0,
            is_admin=False,
            allowed_tools=[],
            knowledge_scope_tags=[],
            visible_kb_ids=[],
            retrieval_filters=[],
            decision="denied",
            detail=f"拒绝：{e}",
            **agent_ctx,
        )

    subject = subject_from_capability(cap)
    filters = build_acl_filters(subject)
    allowed_specs = filter_tools(cap.allowed_tools)

    if payload.action == "tool_call":
        if payload.tool_name is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "action=tool_call 需提供 tool_name")
        allowed = ("*" in cap.allowed_tools) or any(
            s.name == payload.tool_name for s in allowed_specs
        )
        decision = "allowed" if allowed else "denied"
        detail = (
            f"工具 {payload.tool_name} 在{scope_label}白名单内，放行"
            if allowed
            else f"工具 {payload.tool_name} 不在{scope_label}白名单内，拒绝"
        )
    else:  # retrieve
        if not cap.knowledge_scope_tags:
            decision, detail = "denied", f"{scope_label}无可见知识域（denied_no_access）"
        elif not filters:
            decision, detail = "allowed", "admin/通配：检索不过滤（全可见）"
        else:
            decision = "allowed"
            detail = f"{scope_label}召回前过滤：" + ", ".join(
                f"{f.field} {f.op} {f.value or f.values}" for f in filters
            )

    return PolicySimulateResult(
        user_id=payload.user_id,
        user_name=user.name,
        role=cap.role,
        level=cap.level,
        is_admin=cap.is_admin,
        allowed_tools=cap.allowed_tools,
        knowledge_scope_tags=cap.knowledge_scope_tags,
        visible_kb_ids=cap.visible_kb_ids,
        retrieval_filters=[
            {"field": f.field, "op": f.op, "value": f.value, "values": f.values} for f in filters
        ],
        decision=decision,
        detail=detail,
        **agent_ctx,
    )


@router.get("/ops-metrics", response_model=dict)
def ops_metrics(
    _admin: None = Depends(_require_admin),
) -> dict:
    """五维运营指标快照（T2.5）：延迟/成本/人工接管率/失败率/重复执行率。

    数据直接读进程内 Prometheus registry（与 /metrics 同一来源），前端看板据此
    展示，也是试点阶段业务效果看板（T3.4）的底子——不再另造埋点。
    """
    from bycall.observability.metrics import ops_metrics_snapshot

    return ops_metrics_snapshot()


def _require_role_exists(session: Session, role_name: str) -> Role:
    """按 name 取角色，不存在则 404。注意 admin 是内置角色，不在表里。"""
    role = session.get(Role, role_name)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"角色不存在: {role_name}",
        )
    return role


def _check_im_bindings_unique(
    session: Session, bindings: dict[str, str], exclude_user_id: int | None = None
) -> None:
    """校验 IM 绑定全局唯一：每个 (channel, im_user_id) 只能绑一个用户。

    遍历全部用户（SQLite 下 JSON 列查询受限，Python 侧过滤），冲突 → 409。
    这闭环了身份绑定：避免同一 IM 账号绑到多个 Bycall 用户导致映射到错误的人。
    """
    if not bindings:
        return
    for channel, im_id in bindings.items():
        existing = session.exec(
            select(IMBinding).where(
                IMBinding.channel == channel,
                IMBinding.im_user_id == im_id,
            )
        ).first()
        if existing is not None and existing.user_id != exclude_user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"IM 账号 {channel}:{im_id} 已绑定其他用户",
            )


def _replace_im_bindings(session: Session, user_id: int, bindings: dict[str, str]) -> None:
    """同步规范化绑定表；唯一约束负责兜住并发写入竞争。"""
    existing = session.exec(select(IMBinding).where(IMBinding.user_id == user_id)).all()
    for binding in existing:
        session.delete(binding)
    session.flush()
    for channel, im_id in bindings.items():
        if not channel.strip() or not im_id.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "IM channel 和 user_id 不能为空")
        session.add(IMBinding(user_id=user_id, channel=channel.strip(), im_user_id=im_id.strip()))


# ============ 用户 ============


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> User:
    """创建用户。密码哈希后入库；name 唯一，冲突 → 409。IM 绑定校验全局唯一。"""
    # 校验 IM 绑定唯一（闭环身份映射，避免同一 IM 账号绑多个用户）。
    if payload.im_bindings:
        _check_im_bindings_unique(session, payload.im_bindings)
    user = User(
        name=payload.name,
        display_name=payload.display_name,
        role=payload.role,
        password_hash=hash_password(payload.password) if payload.password else "",
        is_active=payload.is_active,
        im_bindings=payload.im_bindings or {},
    )
    session.add(user)
    try:
        session.flush()
        _replace_im_bindings(session, user.id, user.im_bindings)
        session.commit()
    except IntegrityError as exc:
        # name 唯一约束冲突（其他唯一键目前没有）。
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"用户名已存在: {payload.name}",
        ) from exc
    session.refresh(user)
    _audit_admin(session, request, "create_user", f"User({user.name})", after=user)
    session.commit()
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[User]:
    """列出全部用户（含已停用的，便于审计）。按 id 升序。"""
    users = session.exec(select(User).order_by(User.id)).all()
    return list(users)


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> User:
    """按 id 取用户详情，不存在 → 404。"""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在: {user_id}",
        )
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> User:
    """更新用户。改密码重新 hash；改 role 校验目标角色存在（admin 内置角色放行）。

    PATCH 语义：仅传入字段被改。
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在: {user_id}",
        )

    # 审计：记录变更前快照
    before_snapshot = json.dumps(user.model_dump(), default=str)

    if payload.display_name is not None:
        user.display_name = payload.display_name

    if payload.role is not None and payload.role != user.role:
        # 切到内置 admin 角色，或目标 Role 表记录存在，才允许。
        if payload.role not in _RESERVED_ROLE_NAMES:
            _require_role_exists(session, payload.role)
        user.role = payload.role

    if payload.is_active is not None:
        user.is_active = payload.is_active

    if payload.password is not None:
        user.password_hash = hash_password(payload.password)

    # 更新 IM 绑定（校验全局唯一，排除当前用户自己）。
    if payload.im_bindings is not None:
        _check_im_bindings_unique(session, payload.im_bindings, exclude_user_id=user.id)
        user.im_bindings = payload.im_bindings
        _replace_im_bindings(session, user.id, payload.im_bindings)

    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="IM 绑定已被其他用户占用",
        ) from exc
    session.refresh(user)
    _audit_admin(
        session,
        request,
        "update_user",
        f"User({user.name})",
        before=json.loads(before_snapshot),
        after=user,
    )
    session.commit()
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """软删除用户：置 ``is_active=False``，不物理删除（保留审计完整性）。

    禁止删除 ``admin`` 角色账号（防止误锁出超级管理员，导致系统无人可管）→ 400。
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在: {user_id}",
        )
    if user.role in _RESERVED_ROLE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="禁止删除内置 admin 账号",
        )
    user.is_active = False
    session.add(user)
    session.commit()
    _audit_admin(session, request, "delete_user", f"User({user.name})", before=user)
    session.commit()


# ============ 角色 ============


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Role:
    """创建角色。``admin`` 是保留字，不能建 → 400；name 已存在 → 409。"""
    if payload.name in _RESERVED_ROLE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'admin' 是内置保留角色名，不能创建",
        )
    role = Role(
        name=payload.name,
        description=payload.description,
        config=payload.config,
    )
    session.add(role)
    try:
        session.commit()
    except IntegrityError as exc:
        # name 是主键，重复 → 冲突。
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"角色已存在: {payload.name}",
        ) from exc
    session.refresh(role)
    _audit_admin(session, request, "create_role", f"Role({role.name})", after=role)
    session.commit()
    return role


@router.get("/roles", response_model=list[RoleOut])
def list_roles(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[Role]:
    """列出全部角色（按 name 升序）。"""
    roles = session.exec(select(Role).order_by(Role.name)).all()
    return list(roles)


@router.patch("/roles/{name}", response_model=RoleOut)
def update_role(
    name: str,
    payload: RoleUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Role:
    """更新角色的 description / config。不存在 → 404。"""
    role = _require_role_exists(session, name)
    before_snapshot = json.dumps(role.model_dump(), default=str)
    if payload.description is not None:
        role.description = payload.description
    if payload.config is not None:
        role.config = payload.config
    session.add(role)
    session.commit()
    session.refresh(role)
    _audit_admin(
        session,
        request,
        "update_role",
        f"Role({role.name})",
        before=json.loads(before_snapshot),
        after=role,
    )
    session.commit()
    return role


@router.delete("/roles/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除角色。若仍有用户引用该角色 → 409（避免悬空引用）。

    内置保留字 ``admin`` 不在表里，删除自然 404。
    """
    role = _require_role_exists(session, name)
    # 校验无活跃用户引用该角色（停用用户允许删，因其不再生效）。
    ref_count = session.exec(
        select(User).where(User.role == name, User.is_active == True)  # noqa: E712
    ).all()
    if ref_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"角色 '{name}' 仍被 {len(ref_count)} 个活跃用户引用，无法删除",
        )
    session.delete(role)
    session.commit()
    _audit_admin(session, request, "delete_role", f"Role({name})", before={"name": name})


# ============ 系统提示词 ============


@router.post("/prompts", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
def create_prompt(
    payload: PromptCreate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> SystemPrompt:
    """创建新版本提示词。version 唯一，冲突 → 409。

    若创建时 ``is_active=True``，会把其他版本置 False（保持全局唯一 active）。
    """
    if payload.is_active:
        # 先把既有 active 全部置 False，保证全局只有一条 active。
        _deactivate_all_prompts(session)

    prompt = SystemPrompt(
        version=payload.version,
        label=payload.label,
        content=payload.content,
        is_active=payload.is_active,
    )
    session.add(prompt)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"提示词版本已存在: {payload.version}",
        ) from exc
    session.refresh(prompt)
    return prompt


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[SystemPrompt]:
    """列出全部提示词版本，按 version 倒序（最新在前）。"""
    prompts = session.exec(select(SystemPrompt).order_by(SystemPrompt.version.desc())).all()
    return list(prompts)


@router.post("/prompts/activate", response_model=PromptOut)
def activate_prompt(
    payload: PromptActivate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> SystemPrompt:
    """激活指定版本：先把所有版本 ``is_active`` 置 False，再把目标置 True。

    用同一个 session 的单次 commit 保证原子性（要么全成，要么全回滚）。
    目标版本不存在 → 404。
    """
    target = session.exec(
        select(SystemPrompt).where(SystemPrompt.version == payload.version)
    ).first()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"提示词版本不存在: {payload.version}",
        )

    _deactivate_all_prompts(session)
    target.is_active = True
    session.add(target)
    session.commit()
    session.refresh(target)
    return target


def _deactivate_all_prompts(session: Session) -> None:
    """把所有 SystemPrompt 的 is_active 置 False（不单独 commit，由调用方统一提交）。"""
    for prompt in session.exec(select(SystemPrompt)).all():
        if prompt.is_active:
            prompt.is_active = False
            session.add(prompt)


@router.get("/prompts/{version}", response_model=PromptOut)
def get_prompt(
    version: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> SystemPrompt:
    """按版本号查单条提示词。"""
    prompt = session.exec(select(SystemPrompt).where(SystemPrompt.version == version)).first()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"提示词版本不存在: {version}")
    return prompt


@router.put("/prompts/{version}", response_model=PromptOut)
def update_prompt(
    version: int,
    payload: PromptUpdate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> SystemPrompt:
    """更新提示词版本（PATCH 语义：只改传入字段）。"""
    prompt = session.exec(select(SystemPrompt).where(SystemPrompt.version == version)).first()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"提示词版本不存在: {version}")

    if payload.version is not None:
        # 改版本号 → 检查唯一性
        existing = session.exec(
            select(SystemPrompt).where(SystemPrompt.version == payload.version)
        ).first()
        if existing is not None and existing.id != prompt.id:
            raise HTTPException(status.HTTP_409_CONFLICT, f"提示词版本已存在: {payload.version}")
        prompt.version = payload.version
    if payload.label is not None:
        prompt.label = payload.label
    if payload.content is not None:
        prompt.content = payload.content

    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    return prompt


@router.delete("/prompts/{version}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prompt(
    version: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Response:
    """删除提示词版本。不会自动激活其他版本——admin 自行决定后续激活。"""
    prompt = session.exec(select(SystemPrompt).where(SystemPrompt.version == version)).first()
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"提示词版本不存在: {version}")
    session.delete(prompt)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============ 发布门 + 灰度 + 回滚（T2.4，prompt / model 共用） ============

_VALID_RELEASE_KINDS = frozenset({"prompt", "model"})


def _run_publish_gate(kind: str):
    """跑发布门（离线评测用例，任一失败即阻断）。返回 GateReport。"""
    from bycall.evals.gate import run_release_gate

    return run_release_gate(kind)


def _raise_gate_failed(kind: str, gate_report) -> None:
    """发布门未通过 → 409，且**不改任何 is_active**（失败即阻断）。"""
    detail = {
        "message": f"发布门未通过：{kind} 变更被阻断（任一失败用例即红灯）",
        "gate": gate_report.to_dict(),
    }
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _release_response(release: ReleaseState, gate_report=None) -> dict:
    """发布端点统一响应：发布状态 + 门判定报告。"""
    out = ReleaseOut.model_validate(release).model_dump(mode="json")
    return {"ok": True, "release": out, "gate": gate_report.to_dict() if gate_report else None}


@router.get("/releases", response_model=list[ReleaseOut])
def list_releases(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[ReleaseState]:
    """列出发布状态（prompt/model 的灰度/回滚生命周期）。"""
    return list(session.exec(select(ReleaseState).order_by(ReleaseState.kind)).all())


@router.post("/releases/{kind}/promote", response_model=ReleaseOut)
def promote_release_endpoint(
    kind: str,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ReleaseState:
    """灰度转全量：把 is_active 切到发布目标，状态 → active。"""
    if kind not in _VALID_RELEASE_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"发布类型不存在: {kind}")
    from bycall.governance.release import promote_release

    try:
        release = promote_release(session, kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if release is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"没有进行中的 {kind} 发布")
    _audit_admin(
        session, request, f"promote_{kind}_release", f"ReleaseState({kind})", after=release
    )
    session.commit()
    session.refresh(release)
    return release


@router.post("/releases/{kind}/rollback", response_model=ReleaseOut)
def rollback_release_endpoint(
    kind: str,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ReleaseState:
    """一键回滚：is_active 切回上一版本/模型（复用 version + is_active 模式）。"""
    if kind not in _VALID_RELEASE_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"发布类型不存在: {kind}")
    from bycall.governance.release import rollback_release

    try:
        release = rollback_release(session, kind)
    except ValueError as exc:
        # 无上一版本 / 上一版本已被删除 → 409，且 is_active 保持原状（fail-closed）。
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if release is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"没有可回滚的 {kind} 发布")
    _audit_admin(
        session, request, f"rollback_{kind}_release", f"ReleaseState({kind})", after=release
    )
    session.commit()
    session.refresh(release)
    return release


@router.post("/prompts/{version}/publish", response_model=dict)
def publish_prompt_version(
    version: int,
    payload: ReleasePublishRequest,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict:
    """发布提示词版本：过评测门 → 灰度或全量切换。

    一个失败用例即阻断（409），此时 is_active 保持原状——失败即无副作用。
    """
    target = session.exec(select(SystemPrompt).where(SystemPrompt.version == version)).first()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"提示词版本不存在: {version}")
    active = session.exec(SystemPrompt.select_active()).first()
    if active is not None and active.version == version:
        raise HTTPException(status.HTTP_409_CONFLICT, f"版本 {version} 已是当前激活版本")

    gate_report = None
    if not payload.skip_gate:
        gate_report = _run_publish_gate("prompt")
        if not gate_report.passed:
            _raise_gate_failed("prompt", gate_report)

    from bycall.governance.release import activate_target, upsert_release

    release = upsert_release(
        session,
        kind="prompt",
        target_prompt_version=version,
        previous_prompt_version=active.version if active is not None else None,
        canary_percent=payload.canary_percent,
        gate_report_json=json.dumps(gate_report.to_dict()) if gate_report else "{}",
    )
    if payload.canary_percent in (0, 100):
        # 全量：原子切换 is_active（复用现有 activate 语义）。
        activate_target(session, release)
    # 灰度（1–99）：不切 is_active——运行时按用户稳定哈希分流到 target。
    session.commit()
    session.refresh(release)
    _audit_admin(
        session,
        request,
        "publish_prompt",
        f"SystemPrompt(v{version})",
        before={"previous_active_version": active.version if active else None},
        after=release,
    )
    session.commit()
    return _release_response(release, gate_report)


# ============ Skill（领域知识文档，对齐 OpenClaw SKILL）============


@router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
def create_skill(
    payload: SkillCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Skill:
    """创建 Skill。``name`` 全局唯一，冲突 → 409。

    Skill 是 admin 管控的领域知识文档（对齐 OpenClaw SKILL），注入 system prompt 指导
    LLM 完成某领域任务。默认 ``is_enabled=True``，创建时可显式指定（对齐前端启用开关）。
    """
    skill = Skill(
        name=payload.name,
        description=payload.description,
        content=payload.content,
        is_enabled=payload.is_enabled,
    )
    if payload.disclosure is not None:
        skill.disclosure = payload.disclosure
    session.add(skill)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Skill 名称已存在: {payload.name}",
        ) from exc
    session.refresh(skill)
    _audit_admin(session, request, "create_skill", f"Skill({skill.name})", after=skill)
    session.commit()
    return skill


@router.get("/skills", response_model=list[SkillOut])
def list_skills(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[Skill]:
    """列出全部 Skill（含禁用的，按 name 排序）。admin 目录用。"""
    return list(session.exec(Skill.select_all()).all())


# ============ Skill 导入导出 ============


@router.get("/skills/export")
def export_skills(
    names: list[str] | None = Query(
        default=None, description="要导出的 skill name 列表，不传=全部"
    ),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
):
    """导出 Skill 为 ZIP（Agent Skills 规范格式）。

    每个 skill 对应 zip 内一个目录 ``{name}/SKILL.md``（YAML frontmatter + Markdown body）。
    返回 ``application/zip`` 流式下载响应。
    """
    import io

    from bycall.admin.skill_io import export_skills_to_zip

    if names:
        skills = list(session.exec(Skill.select_all()).all())
        skills = [s for s in skills if s.name in names]
        missing = set(names) - {s.name for s in skills}
        if missing:
            raise HTTPException(
                status_code=404, detail=f"Skill 不存在: {', '.join(sorted(missing))}"
            )
    else:
        skills = list(session.exec(Skill.select_all()).all())

    buf = io.BytesIO()
    export_skills_to_zip(skills, buf)
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="skills-export.zip"'},
    )


@router.post("/skills/import", response_model=SkillImportResult)
def import_skills(
    file: UploadFile,
    request: Request,
    on_conflict: str = Query(
        default="skip", description="冲突策略：skip=跳过已有 / overwrite=覆盖"
    ),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
):
    """导入 Skill ZIP 包。

    解析 zip 内所有 ``{name}/SKILL.md``（Agent Skills 规范格式），按 name 合并到 DB。
    - ``on_conflict=skip``（默认）：name 已存在 → 跳过，不覆盖。
    - ``on_conflict=overwrite``：name 已存在 → 覆盖 content/description/disclosure，保留 created_at。

    返回导入结果汇总（各条 created/updated/skipped + 解析错误）。
    """
    if on_conflict not in ("skip", "overwrite"):
        raise HTTPException(status_code=400, detail="on_conflict 仅支持 skip 或 overwrite")

    from bycall.admin.skill_io import import_skills_from_zip

    raw = file.file.read()
    parsed = import_skills_from_zip(raw, on_conflict)

    result = SkillImportResult()

    for err in parsed.get("errors", []):
        result.errors.append(SkillImportError(entry=err["entry"], reason=err["reason"]))

    for skill_data in parsed.get("skills", []):
        name = skill_data["name"]
        existing = session.exec(Skill.select_by_name(name)).first()
        if existing is not None:
            if on_conflict == "skip":
                result.items.append(
                    SkillImportItem(name=name, status="skipped", message="名称已存在")
                )
                result.total_skipped += 1
                continue
            # overwrite
            existing.description = skill_data.get("description", existing.description)
            existing.content = skill_data.get("content", "")
            existing.disclosure = skill_data.get("disclosure", "always")
            existing.is_enabled = skill_data.get("is_enabled", True)
            session.add(existing)
            session.flush()
            result.items.append(SkillImportItem(name=name, status="updated", message="已覆盖"))
            result.total_updated += 1
        else:
            new_skill = Skill(
                name=name,
                description=skill_data.get("description", ""),
                content=skill_data.get("content", ""),
                disclosure=skill_data.get("disclosure", "always"),
                is_enabled=skill_data.get("is_enabled", True),
            )
            session.add(new_skill)
            session.flush()
            result.items.append(SkillImportItem(name=name, status="created", message="新创建"))
            result.total_created += 1

    # 审计：导入整体记录
    session.commit()
    _audit_admin(
        session,
        request,
        "import_skills",
        f"{len(parsed.get('skills', []))} skills from zip (on_conflict={on_conflict})",
        after={
            "created": result.total_created,
            "updated": result.total_updated,
            "skipped": result.total_skipped,
            "errors": len(result.errors),
        },
    )
    session.commit()
    return result


@router.get("/skills/{name}", response_model=SkillOut)
def get_skill(
    name: str,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Skill:
    """Skill 详情。不存在 → 404。"""
    skill = session.exec(Skill.select_by_name(name)).first()
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill 不存在: {name}")
    return skill


@router.put("/skills/{name}", response_model=SkillOut)
def update_skill(
    name: str,
    payload: SkillUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Skill:
    """更新 Skill（PATCH 语义：只改传入字段）。可迭代文档，无需发新版本。不存在 → 404。

    改 content / is_enabled 后，下次对话即时生效（DB 即真值，capability 引擎实时读）。
    """
    skill = session.exec(Skill.select_by_name(name)).first()
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill 不存在: {name}")

    before_snapshot = json.dumps(skill.model_dump(), default=str)
    if payload.description is not None:
        skill.description = payload.description
    if payload.content is not None:
        skill.content = payload.content
    if payload.is_enabled is not None:
        skill.is_enabled = payload.is_enabled
    if payload.disclosure is not None:
        skill.disclosure = payload.disclosure
    session.add(skill)
    session.commit()
    session.refresh(skill)
    _audit_admin(
        session,
        request,
        "update_skill",
        f"Skill({skill.name})",
        before=json.loads(before_snapshot),
        after=skill,
    )
    session.commit()
    return skill


@router.delete("/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除 Skill。不存在 → 404。若被角色引用 → 409（避免悬空引用）。

    引用检查：扫所有 Role 的 ``RoleConfig.skills``，若含该 name 则拒绝删除
    （admin 需先从角色配置里移除引用）。capability 引擎对不存在的 skill name 会
    静默忽略，但显式拒绝删除能让 admin 意识到影响范围。
    """
    skill = session.exec(Skill.select_by_name(name)).first()
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill 不存在: {name}")

    # 校验无角色引用（与 delete_role 校验活跃用户的范式一致）。
    referencing_roles = [
        r.name for r in session.exec(select(Role)).all() if name in (r.config.skills or [])
    ]
    if referencing_roles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Skill '{name}' 仍被角色 {referencing_roles} 引用，"
                "请先从这些角色的 skills 配置里移除"
            ),
        )
    # 删除前抓纯 dict 快照（detached-safe：delete+commit 后对象进 detached 态，
    # 显式抓 dict 比 model_dump 恰好能读更稳，与 update_* 审计范式一致）。
    before_snapshot = skill.model_dump()
    session.delete(skill)
    session.commit()
    _audit_admin(session, request, "delete_skill", f"Skill({name})", before=before_snapshot)


# ============ 审计 ============


@router.get("/audit", response_model=AuditPage | list[AuditOut])
def list_audit(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    user_id: int | None = Query(default=None, ge=1, description="按用户 ID 筛选"),
    decision: str | None = Query(default=None, description="按决策筛选"),
    limit: int | None = Query(
        default=None, ge=1, le=500, description="兼容旧客户端：返回最近 N 条数组"
    ),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> AuditPage | list[AuditLog]:
    """分页返回审计记录；带旧 ``limit`` 参数时保持数组响应兼容。"""
    filters = []
    if user_id is not None:
        filters.append(AuditLog.user_id == user_id)
    if decision:
        filters.append(AuditLog.decision == decision)

    statement = select(AuditLog)
    count_statement = select(func.count()).select_from(AuditLog)
    for condition in filters:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    ordered_statement = statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if limit is not None:
        return list(session.exec(ordered_statement.limit(limit)).all())

    total = session.exec(count_statement).one()
    logs = session.exec(ordered_statement.offset((page - 1) * page_size).limit(page_size)).all()
    return AuditPage(items=list(logs), total=total, page=page, page_size=page_size)


# ============ 运行时设置（LLM/IM 凭证，控制台管理）============


@router.get("/settings", response_model=list[SettingOut])
def list_settings(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[dict]:
    """返回所有运行时设置。secret 字段脱敏（绝不回显明文）。"""
    from bycall.governance.secrets import decrypt_secret
    from bycall.governance.settings_store import mask_secret

    rows = session.exec(SystemSetting.select_all()).all()
    out = []
    for r in rows:
        plain_value = decrypt_secret(r.value) if r.is_secret else r.value
        value = mask_secret(plain_value) if r.is_secret else plain_value
        out.append(
            {
                "key": r.key,
                "value": value,
                "is_secret": r.is_secret,
                "description": r.description,
                "is_set": bool(plain_value),
            }
        )
    return out


@router.put("/settings", response_model=dict)
def update_settings(
    body: SettingsBatchUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict:
    """批量更新设置。admin 改完下一条消息即时生效（无需重启）。

    - secret 字段：value 为空串或脱敏占位符（含 ***）时视为"未改"，跳过
      （前端未改的 secret 回传空串，见 Settings.vue「不改则留空保存」）。
    - 非秘密 value 空串：清除该 key（回退到 .env/默认）。
    """
    from bycall.governance.settings_store import set_setting_value

    skipped: list[str] = []
    updated: list[str] = []
    for key, value in body.settings.items():
        existing = session.exec(SystemSetting.select_by_key(key)).first()
        if existing is None:
            skipped.append(f"{key}（未知 key）")
            continue
        # 解析设置写入校验：非法值直接 4xx（避免脏数据入库后被运行时静默回退）
        if key.startswith("parse.") and value != "":
            _validate_parse_setting(key, value)
        # secret 保护：空串或脱敏占位符（含 ***）→ 用户没真改，跳过。
        # （前端未改的 secret 回传空串；若当 clear 处理会清掉已配置 secret，
        #   曾导致企微鉴权 errcode=853000 invalid bot_id or secret。）
        if existing.is_secret and (value == "" or "***" in value):
            skipped.append(f"{key}（未改）")
            continue
        set_setting_value(
            session,
            key,
            value,
            is_secret=existing.is_secret,
            description=existing.description,
        )
        updated.append(key)
    session.commit()
    _audit_admin(
        session,
        request,
        "update_settings",
        ",".join(updated),
        after={"updated": updated, "skipped": skipped},
    )
    session.commit()
    return {"ok": True, "updated": updated, "skipped": skipped}


# 解析设置合法值（与 config.py 的 ParseConfig 保持一致）
_PARSE_ENGINE_VALUES = ("builtin", "markitdown", "mineru")
_PARSE_METHOD_VALUES = ("auto", "txt", "ocr")
_PARSE_MINERU_MODE_VALUES = ("selfhost", "cloud")
_PARSE_MINERU_MODEL_VALUES = ("pipeline", "vlm", "MinerU-HTML")


def _validate_parse_setting(key: str, value: str) -> None:
    """校验 parse.* 设置写入；非法值 4xx 拒绝。

    空串不在此校验（沿用「空串 = 清除该 key、回退 .env/默认」的统一语义）。
    """

    def _reject(reason: str) -> None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key}: {reason}")

    if key == "parse.engine":
        if value not in _PARSE_ENGINE_VALUES:
            _reject(f"必须是 {' / '.join(_PARSE_ENGINE_VALUES)} 之一")
    elif key == "parse.mineru_mode":
        if value not in _PARSE_MINERU_MODE_VALUES:
            _reject(f"必须是 {' / '.join(_PARSE_MINERU_MODE_VALUES)} 之一")
    elif key == "parse.mineru_model_version":
        if value not in _PARSE_MINERU_MODEL_VALUES:
            _reject(f"必须是 {' / '.join(_PARSE_MINERU_MODEL_VALUES)} 之一")
    elif key == "parse.mineru_parse_method":
        if value not in _PARSE_METHOD_VALUES:
            _reject(f"必须是 {' / '.join(_PARSE_METHOD_VALUES)} 之一")
    elif key in ("parse.markitdown_enable_plugins", "parse.mineru_is_ocr"):
        if value.strip().lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            _reject("必须是 true / false")
    elif key in ("parse.mineru_timeout", "parse.mineru_poll_interval"):
        try:
            num = float(value)
        except ValueError:
            _reject("必须是数字")
        if num <= 0:
            _reject("必须大于 0")


# ============ IM 渠道热重载 ============


@router.post("/im/reload", response_model=dict)
def reload_im_channel(
    agent_id: int | None = Query(default=None, description="指定则只重载该数字员工的专属 Bot"),
    _admin: None = Depends(_require_admin),
) -> dict:
    """热重载 IM 渠道：断开旧连接 + 用当前凭证建新连接。不重启 HTTP 服务。

    admin 改完 IM 凭证后调用此端点（或前端保存设置时自动触发）。
    借鉴 OpenClaw 的 channel reload：只重启那一个渠道，其他不动。
    带 ``agent_id`` 时只重载该实例的专属 Bot 槽位（ADR-0012）；不带则重载全局默认渠道。
    """
    from bycall.gateway.manager import get_channel_manager

    mgr = get_channel_manager()
    if agent_id is not None:
        ok, msg = mgr.reload_agent(agent_id)
    else:
        ok, msg = mgr.reload()
    return {"ok": ok, "message": msg, "status": mgr.status()}


@router.get("/im/status", response_model=dict)
def im_status(
    _admin: None = Depends(_require_admin),
) -> dict:
    """查询全部 IM 连接状态：顶层为全局默认渠道（兼容 /ready），``channels`` 逐槽位明细。

    每个槽位含 ``scope``（global/agent）、``agent_id``、``bot_id``、连接状态与最近错误，
    供渠道接入页展示多 Bot 连接总览（ADR-0012）。
    """
    from bycall.gateway.manager import get_channel_manager

    return get_channel_manager().status()


# ============ dev act-as（演示权限链路用）============


@router.post("/act-as", tags=["session"])
def act_as(
    request: Request,
    response: Response,
    target_user_id: int = Query(..., description="要扮演的用户 id"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict[str, bool]:
    """admin 扮演某普通用户（dev only）。返回新 cookie。

    便于在无真实 IM 时验证数据面权限链路。生产环境禁止。
    """
    from bycall.governance.auth import load_session_cookie, make_session_cookie, set_act_as

    token = request.cookies.get("bycall_session")
    sess_data = load_session_cookie(token) if token else None
    if sess_data is None or not sess_data.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")

    admin = session.get(User, sess_data.user_id)
    if admin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admin 用户不存在")
    try:
        new_data = set_act_as(session, admin, target_user_id)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    response.set_cookie(
        "bycall_session",
        make_session_cookie(new_data),
        httponly=True,
        samesite="lax",
        max_age=86400,
    )
    return {"ok": True}


@router.post("/clear-act-as", tags=["session"])
def clear_act_as_route(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict[str, bool]:
    """清除扮演，回到 admin 身份。"""
    from bycall.governance.auth import clear_act_as, load_session_cookie, make_session_cookie

    token = request.cookies.get("bycall_session")
    sess_data = load_session_cookie(token) if token else None
    if sess_data is None or not sess_data.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")

    admin = session.get(User, sess_data.user_id)
    if admin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "admin 用户不存在")
    new_data = clear_act_as(session, admin)
    response.set_cookie(
        "bycall_session",
        make_session_cookie(new_data),
        httponly=True,
        samesite="lax",
        max_age=86400,
    )
    return {"ok": True}


# ============ 模型实体管理（参考 WeKnora，CRUD + 激活 + 测试 + provider 注册表）============


def _model_to_out(model: Model) -> ModelOut:
    """把 Model ORM 行转 ModelOut：api_key 脱敏 + has_api_key 标记。绝不回显明文。"""
    from bycall.governance.secrets import decrypt_secret
    from bycall.governance.settings_store import mask_secret

    api_key = decrypt_secret(model.api_key)

    return ModelOut(
        id=model.id,  # type: ignore[arg-type]
        name=model.name,
        display_name=model.display_name,
        model_type=model.model_type,
        source=model.source,
        provider=model.provider,
        base_url=model.base_url,
        api_key_masked=mask_secret(api_key),
        has_api_key=bool(api_key),
        embed_dimension=model.embed_dimension,
        is_active=model.is_active,
        description=model.description,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _get_model_or_404(session: Session, model_id: int) -> Model:
    """按 id 取模型，不存在 → 404。"""
    model = session.get(Model, model_id)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"模型不存在: {model_id}",
        )
    return model


def _deactivate_same_type(session: Session, model_type: str) -> None:
    """把同 model_type 的其他模型 is_active 全置 False（不单独 commit，由调用方提交）。"""
    for m in session.exec(Model.select_by_type(model_type)).all():
        if m.is_active:
            m.is_active = False
            session.add(m)


@router.get("/models/providers", response_model=list[dict])
def list_model_providers(
    model_type: str | None = Query(
        default=None, description="按模型类型过滤（chat/embedding/rerank）"
    ),
    _admin: None = Depends(_require_admin),
) -> list[dict]:
    """provider 注册表：前端新建模型时填 provider 下拉 + 自动填默认 URL + 推荐模型名。"""
    from bycall.llm.providers import list_providers, to_dict_list

    return to_dict_list(list_providers(model_type))


@router.get("/models", response_model=list[ModelOut])
def list_models(
    model_type: str | None = Query(
        default=None, description="按模型类型过滤（chat/embedding/rerank）"
    ),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[ModelOut]:
    """列出模型。可选 model_type 过滤。api_key 一律脱敏，不回显明文。"""
    if model_type is not None:
        rows = session.exec(Model.select_by_type(model_type)).all()
    else:
        rows = session.exec(select(Model).order_by(Model.id)).all()
    return [_model_to_out(m) for m in rows]


@router.post("/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
def create_model(
    payload: ModelCreate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ModelOut:
    """创建模型。name + model_type 联合冲突 → 409。

    若 ``is_active=True``，先把同类型其他模型置 False（保持同类型唯一 active）。
    注：表 schema 未建复合唯一索引，这里在应用层显式预检（同类型下重名才视为冲突，
    不同类型允许同名）。
    """
    existing = session.exec(
        select(Model).where(Model.name == payload.name, Model.model_type == payload.model_type)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"同类型下模型名已存在: {payload.name} ({payload.model_type})",
        )

    if payload.is_active:
        _deactivate_same_type(session, payload.model_type)

    from bycall.governance.secrets import encrypt_secret

    model = Model(
        name=payload.name,
        display_name=payload.display_name,
        model_type=payload.model_type,
        source=payload.source,
        provider=payload.provider,
        base_url=payload.base_url,
        api_key=encrypt_secret(payload.api_key),
        embed_dimension=payload.embed_dimension,
        description=payload.description,
        is_active=payload.is_active,
    )
    session.add(model)
    session.commit()
    session.refresh(model)
    return _model_to_out(model)


@router.get("/models/{model_id}", response_model=ModelOut)
def get_model(
    model_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ModelOut:
    """按 id 取模型详情，不存在 → 404。api_key 脱敏。"""
    return _model_to_out(_get_model_or_404(session, model_id))


@router.put("/models/{model_id}", response_model=ModelOut)
def update_model(
    model_id: int,
    payload: ModelUpdate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ModelOut:
    """更新模型（PATCH 语义：仅传入字段生效）。

    ``api_key`` 若含 ``***``（前端回填的脱敏占位符）视为"未改"，跳过——
    避免把展示用的脱敏串当真实密钥写回去覆盖原值。
    """
    model = _get_model_or_404(session, model_id)

    if payload.display_name is not None:
        model.display_name = payload.display_name
    if payload.provider is not None:
        model.provider = payload.provider
    if payload.base_url is not None:
        model.base_url = payload.base_url
    if payload.embed_dimension is not None:
        model.embed_dimension = payload.embed_dimension
    if payload.description is not None:
        model.description = payload.description
    if payload.api_key is not None and "***" not in payload.api_key:
        # 含 *** 的脱敏占位符（前端回填）→ 用户没真改密钥，跳过；否则更新。
        from bycall.governance.secrets import encrypt_secret

        model.api_key = encrypt_secret(payload.api_key)

    session.add(model)
    session.commit()
    session.refresh(model)
    return _model_to_out(model)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(
    model_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除模型。若存在知识库/会话引用该模型，删除后会降级到默认行为。"""
    model = _get_model_or_404(session, model_id)
    _audit_admin(session, request, "delete_model", f"Model({model.name})", before=model)
    session.delete(model)
    session.commit()


@router.post("/models/{model_id}/activate", response_model=dict)
def activate_model(
    model_id: int,
    request: Request,
    rebuild: bool = Query(
        default=False,
        description="embedding 维度变化时是否确认重建知识库索引",
    ),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict:
    """激活指定模型：同 model_type 原子切换（其他置 False，本条置 True）。

    embedding 维度检测（关键死结）：
    - 若新激活的 embedding 模型 ``embed_dimension`` 与当前 active 的不同，
      且未带 ``rebuild=true``，返回 ``{"need_rebuild": True, ...}`` 让前端二次确认；
    - 带 ``rebuild=true`` 才真正切换（重建知识库索引由调用方/前端另行触发，本端只切模型）。
    """
    model = _get_model_or_404(session, model_id)

    if model.model_type == "embedding" and not rebuild:
        current = session.exec(Model.select_active_by_type("embedding")).first()
        if (
            current is not None
            and current.id != model.id
            and current.embed_dimension != model.embed_dimension
        ):
            return {
                "need_rebuild": True,
                "old_dim": current.embed_dimension,
                "new_dim": model.embed_dimension,
                "message": "向量维度变化，激活后需重建知识库索引",
            }

    # 原子切换：同类型其他置 False，本条置 True。
    _deactivate_same_type(session, model.model_type)
    model.is_active = True
    session.add(model)
    session.commit()
    session.refresh(model)
    _audit_admin(session, request, "activate_model", f"Model({model.name})", after=model)
    session.commit()
    return {"ok": True, "model": _model_to_out(model).model_dump(mode="json")}


@router.post("/models/{model_id}/publish", response_model=dict)
def publish_model(
    model_id: int,
    payload: ReleasePublishRequest,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict:
    """发布 chat 模型：过评测门 → 灰度或全量切换（回滚复用 is_active 切换）。"""
    model = _get_model_or_404(session, model_id)
    if model.model_type != "chat":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "发布门当前只支持 chat 模型（embedding/rerank 用 /activate 端点）",
        )
    active = session.exec(Model.select_active_by_type("chat")).first()
    if active is not None and active.id == model.id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"模型 {model.name} 已是当前激活模型")

    gate_report = None
    if not payload.skip_gate:
        gate_report = _run_publish_gate("model")
        if not gate_report.passed:
            _raise_gate_failed("model", gate_report)

    from bycall.governance.release import activate_target, upsert_release

    release = upsert_release(
        session,
        kind="model",
        target_model_id=model.id,
        previous_model_id=active.id if active is not None else None,
        canary_percent=payload.canary_percent,
        gate_report_json=json.dumps(gate_report.to_dict()) if gate_report else "{}",
    )
    if payload.canary_percent in (0, 100):
        activate_target(session, release)
    session.commit()
    session.refresh(release)
    _audit_admin(
        session,
        request,
        "publish_model",
        f"Model({model.name})",
        before={"previous_active_model_id": active.id if active else None},
        after=release,
    )
    session.commit()
    return _release_response(release, gate_report)


@router.post("/models/{model_id}/test", response_model=ModelTestResult)
def test_model(
    model_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ModelTestResult:
    """连通性测试：按 provider 构造临时 OpenAI 兼容 client 发一次最小请求，计时。

    - chat：``chat.completions.create`` 一条 "hi"；
    - embedding：``embeddings.create`` 一个 "test"，自动探测真实输出维度并回填到实体
      ``embed_dimension``（维度是模型物理事实，非偏好）；配置错误时自动修正而非报错；
    - 异常一律捕获返回 ``ok=False`` + error，绝不抛 500（前端要拿结果展示）。
    """
    import time

    from bycall.governance.secrets import decrypt_secret

    model = _get_model_or_404(session, model_id)

    try:
        from openai import OpenAI
    except ImportError as exc:  # openai 未装：返失败而非 500
        return ModelTestResult(ok=False, error=f"未安装 openai 客户端: {exc}")

    start = time.perf_counter()
    try:
        client = OpenAI(
            base_url=model.base_url or None,
            api_key=decrypt_secret(model.api_key) or "EMPTY",
            timeout=15,
            # 连通性探测要快速反馈：SDK 默认 2 次指数退避重试会让坏端点拖到 10s+
            max_retries=0,
        )
        if model.model_type == "embedding":
            resp = client.embeddings.create(model=model.name, input="test")
            dim = len(resp.data[0].embedding) if resp.data else 0
            latency_ms = int((time.perf_counter() - start) * 1000)
            if dim == 0:
                return ModelTestResult(
                    ok=False, latency_ms=latency_ms, error="模型未返回向量（resp.data 为空）"
                )
            # 维度是模型的物理事实：自动回填到实体并落库，保证 embed_dimension 永远=真实输出。
            # 仅当实际变化时才写库+审计，避免无谓的 UPDATE（updated_at 有 onupdate 会误弹）。
            old_dim = model.embed_dimension
            if old_dim != dim:
                model.embed_dimension = dim
                session.add(model)
                session.commit()
                session.refresh(model)
                _audit_admin(
                    session,
                    request,
                    "auto_detect_dimension",
                    f"Model({model.name})",
                    before={"embed_dimension": old_dim},
                    after={"embed_dimension": dim},
                )
                session.commit()
            detail = f"维度 {dim}（已自动填入）"
            if old_dim and old_dim != dim:
                detail = f"维度已从 {old_dim} 自动修正为 {dim}"
            detail += f"，model={resp.model}"
            return ModelTestResult(
                ok=True, latency_ms=latency_ms, detail=detail, embed_dimension=dim
            )
        elif model.model_type == "rerank":
            # Rerank 测试：用原始 httpx 发 /rerank 请求（对照硅基流动官方示例）。
            # 不能走 client._client.post()——OpenAI SDK 内部的 httpx.Client.post() 不经过
            # SDK 的 auth 层，Authorization header 没被加上，导致硅基流动返回 401。
            import httpx

            api_key = decrypt_secret(model.api_key)
            base_url = (model.base_url or "").rstrip("/")
            resp = httpx.post(
                f"{base_url}/rerank",
                json={
                    "model": model.name,
                    "query": "test",
                    "documents": ["This is a test document."],
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=client.timeout if client.timeout else 15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            result_count = len(data.get("results", []))
            latency_ms = int((time.perf_counter() - start) * 1000)
            return ModelTestResult(
                ok=True, latency_ms=latency_ms, detail=f"返回 {result_count} 条结果"
            )
        else:
            # chat
            resp = client.chat.completions.create(
                model=model.name,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            text = resp.choices[0].message.content if resp.choices else ""
            return ModelTestResult(ok=True, latency_ms=latency_ms, detail=text[:200])
    except Exception as exc:  # 任何异常（网络/鉴权/模型不存在）→ ok=False
        latency_ms = int((time.perf_counter() - start) * 1000)
        msg = str(exc) or exc.__class__.__name__
        return ModelTestResult(ok=False, latency_ms=latency_ms, error=msg[:500])


# ============ 数字员工实例管理（Agent 全托管：CRUD + 启停 + 默认）============


def _agent_to_out(agent: Agent) -> AgentOut:
    """把 Agent ORM 行转 AgentOut：im_config 解密归一 + 脱敏（按渠道分组）。"""
    from bycall.gateway.channels import agent_channels_from_encrypted

    has_im = bool(agent.im_config_encrypted)
    preview: dict = {}
    data: dict[str, dict[str, str]] = {}
    if has_im:
        try:
            data = agent_channels_from_encrypted(agent.im_config_encrypted)
            # 脱敏：按渠道分组，只露字段名，值打码
            preview = {
                ch: {k: ("***" if v else "") for k, v in fields.items()}
                for ch, fields in data.items()
            }
            if not data:
                # 有密文但解析不出任何渠道（损坏/异常）——不静默当作「未配置」
                preview = {"_error": "<decrypt failed>"}
        except Exception:
            preview = {"_error": "<decrypt failed>"}
    return AgentOut(
        id=agent.id,  # type: ignore[arg-type]
        name=agent.name,
        display_name=agent.display_name,
        description=agent.description,
        avatar_url=agent.avatar_url,
        system_prompt_version=agent.system_prompt_version,
        model_id=agent.model_id,
        skills=agent.skills,
        knowledge_scope_tags=agent.knowledge_scope_tags,
        allowed_tools=agent.allowed_tools,
        visible_roles=agent.visible_roles,
        channel=agent.channel,
        has_im_config=has_im,
        im_channels=sorted(data.keys()),
        im_config_preview=preview,
        approval_policy=agent.approval_policy,
        is_enabled=agent.is_enabled,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _get_agent_or_404(session: Session, agent_id: int) -> Agent:
    """按 id 取 Agent，不存在 → 404。"""
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数字员工不存在: {agent_id}",
        )
    return agent


def _validate_agent_im_config(
    session: Session, agent_id: int | None, im_config: dict | None
) -> None:
    """per-Agent IM 凭证写入校验（ADR-0012 修订：多渠道 + 凭证全局唯一）。

    - 渠道类型必须在 ``SUPPORTED_CHANNEL_TYPES``（未接入连接器的渠道配置了也不会生效，
      写入前拦住，不留「看似生效」的缝隙）；
    - 凭证必填字段齐全（半套凭证的渠道不会建连）；
    - 同一渠道的凭证身份字段（企微 bot_id）跨 Agent、以及与全局默认 Bot
      （SystemSetting ``im.{渠道}_{字段}``）冲突即 400——同一 bot_id 建两条连接会被
      企微单连接策略互踢，必须在写入前拦住。
    """
    from bycall.gateway.channels import (
        CREDENTIAL_ID_FIELDS,
        REQUIRED_FIELDS,
        SUPPORTED_CHANNEL_TYPES,
        agent_channels_from_encrypted,
        credential_id,
        global_credential_setting_key,
        parse_im_config_patch,
    )
    from bycall.governance.settings_store import get_setting_value

    if im_config is None:
        return
    try:
        patch = parse_im_config_patch(im_config)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    if not patch:
        return  # {} = 清空全部渠道，无需校验
    for channel_type, fields in patch.items():
        if not any(fields.values()):
            continue  # 空分组 = 移除该渠道，无需校验
        label = CREDENTIAL_ID_FIELDS.get(channel_type, "凭证")
        if channel_type not in SUPPORTED_CHANNEL_TYPES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"渠道 '{channel_type}' 暂未接入连接器，无法配置专属凭证",
            )
        missing = [f for f in REQUIRED_FIELDS.get(channel_type, ()) if not fields.get(f)]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"渠道 '{channel_type}' 专属凭证缺少必填字段：{'、'.join(missing)}",
            )
        cred = credential_id(channel_type, fields)
        if not cred:
            continue
        # 与全局默认 Bot 冲突检查（env 命名沿用 BYCALL_IM_{渠道}_{字段}）。
        global_key = global_credential_setting_key(channel_type)
        global_env = f"BYCALL_IM_{channel_type}_{CREDENTIAL_ID_FIELDS[channel_type]}".upper()
        global_cred = get_setting_value(global_key, default="", env_var=global_env).strip()
        if global_cred and cred == global_cred:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{channel_type} {label} '{cred}' 与全局默认 Bot 冲突"
                    "（见渠道接入页/运行时设置）"
                ),
            )
        # 与其他数字员工冲突检查（读取时归一，兼容存量旧扁平格式）。
        for other in session.exec(Agent.select_all()).all():
            if agent_id is not None and other.id == agent_id:
                continue
            other_channels = agent_channels_from_encrypted(other.im_config_encrypted)
            if credential_id(channel_type, other_channels.get(channel_type)) == cred:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=(f"{channel_type} {label} '{cred}' 已被数字员工 '{other.name}' 使用"),
                )


def _sync_agent_channel_slot(agent: Agent) -> None:
    """按 Agent 最终状态同步其专属 Bot 槽位（ADR-0012 控制面自动生效）。

    启用且有凭证 → 热重载该槽位（新凭证即刻建连）；否则停止该槽位。
    同步失败只记日志——CRUD 结果不受渠道连接问题影响，管理员可在渠道接入页手动重连。
    """
    from bycall.gateway.manager import get_channel_manager

    try:
        mgr = get_channel_manager()
        if agent.is_enabled and agent.im_config_encrypted:
            _ok, msg = mgr.reload_agent(agent.id)  # type: ignore[arg-type]
        else:
            stopped = mgr.stop_agent(agent.id)  # type: ignore[arg-type]
            msg = "专属 Bot 已停止" if stopped else "无运行中的专属 Bot"
        logger.info("数字员工 '%s' 渠道槽位同步：%s", agent.name, msg)
    except Exception:
        logger.exception("数字员工 '%s' 渠道槽位同步失败（不影响保存结果）", agent.name)


def _validate_agent_refs(session: Session, payload: AgentCreate | AgentUpdate) -> None:
    """校验 Agent 引用的外键有效性（model_id / system_prompt_version / skills）。"""
    # model_id 若指定，必须存在且为 chat 类型
    mid = getattr(payload, "model_id", None)
    if mid is not None:
        model = session.get(Model, mid)
        if model is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"模型不存在: {mid}")
        if model.model_type != "chat":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Agent 只能绑定 chat 模型，当前为 {model.model_type}"
            )
    # system_prompt_version 若指定，必须存在
    ver = getattr(payload, "system_prompt_version", None)
    if ver is not None:
        prompt = session.exec(select(SystemPrompt).where(SystemPrompt.version == ver)).first()
        if prompt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"提示词版本不存在: {ver}")
    # skills 若指定，逐个校验存在且启用（未启用也允许，但给 warning）
    skills = getattr(payload, "skills", None)
    if skills is not None:
        for name in skills:
            sk = session.exec(Skill.select_by_name(name)).first()
            if sk is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"Skill 不存在: {name}")


@router.get("/agents", response_model=list[AgentOut])
def list_agents(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[AgentOut]:
    """列出全部数字员工实例（含禁用，按 id 正序）。"""
    rows = session.exec(Agent.select_all()).all()
    return [_agent_to_out(a) for a in rows]


@router.get("/agents/{agent_id}", response_model=AgentOut)
def get_agent(
    agent_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> AgentOut:
    """查看单个数字员工详情。"""
    return _agent_to_out(_get_agent_or_404(session, agent_id))


def _auto_agent_slug(session: Session, display_name: str) -> str:
    """按显示名生成数字员工的内部标识 slug（管理员无需感知，ADR-0013 修订）。

    slug 不是寻址键（控制面/数据面一律按 ``id`` 寻址），只作审计与排障用的稳定标识，
    因此不要求人类可读：显示名能 ASCII 化就用它（``Sales Assistant`` → ``sales-assistant``），
    中文等纯非 ASCII 名称则退化为 ``agent-<4 位随机>``；同名冲突时追加随机后缀重试。
    """
    base = re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-")[:32]
    if not base or not base[0].isalnum():
        base = "agent"
    candidate = base
    for _ in range(64):
        exists = session.exec(select(Agent).where(Agent.name == candidate)).first()
        if exists is None:
            return candidate
        candidate = f"{base}-{secrets.token_hex(2)}"
    raise HTTPException(status.HTTP_409_CONFLICT, detail="无法生成唯一的数字员工标识，请稍后重试")


def _is_agent_name_conflict(exc: IntegrityError) -> bool:
    """该完整性错误是否就是 slug 唯一键冲突（各方言报错文案不同）。

    SQLite：``UNIQUE constraint failed: agent.name``；Postgres：``duplicate key value
    violates unique constraint "uq_agent_name"``。不能只匹配约束名，也不能只匹配
    ``agent.name``（NOT NULL 失败也带 ``agent.name``，那属另一种真因，必须暴露）。
    """
    msg = str(exc)
    return "uq_agent_name" in msg or "UNIQUE constraint failed: agent.name" in msg


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: AgentCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> AgentOut:
    """创建数字员工实例。``name`` 留空则自动生成内部标识；显式冲突 → 409；引用校验失败 → 404/400。"""
    _validate_agent_refs(session, payload)
    _validate_agent_im_config(session, None, payload.im_config)
    # 内部标识：管理员不指定（无感），按显示名生成；显式传入时沿用不再改
    slug = (payload.name or "").strip() or _auto_agent_slug(session, payload.display_name)
    # 加密 per-Agent IM 覆盖（按渠道归一后写入新格式）
    im_encrypted = ""
    if payload.im_config:
        from bycall.gateway.channels import merge_im_config, parse_im_config_patch
        from bycall.governance.secrets import encrypt_secret

        merged = merge_im_config({}, parse_im_config_patch(payload.im_config))
        if merged:
            im_encrypted = encrypt_secret(json.dumps(merged, ensure_ascii=False))
    agent = Agent(
        name=slug,
        display_name=payload.display_name,
        description=payload.description,
        avatar_url=payload.avatar_url,
        system_prompt_version=payload.system_prompt_version,
        model_id=payload.model_id,
        skills=payload.skills,
        knowledge_scope_tags=payload.knowledge_scope_tags,
        allowed_tools=payload.allowed_tools,
        visible_roles=payload.visible_roles,
        channel=payload.channel,
        im_config_encrypted=im_encrypted,
        approval_policy=payload.approval_policy,
        is_enabled=payload.is_enabled,
    )
    try:
        session.add(agent)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # 只有 slug 唯一键冲突才算「已存在」；其他完整性错误（如存量库历史列约束）必须露出真因
        # 并落日志——曾经把 agent.is_default 的 NOT NULL 失败伪装成「数字员工已存在」，白查一轮。
        if _is_agent_name_conflict(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"数字员工已存在: {slug}"
            ) from exc
        logger.exception("创建数字员工失败（非唯一键冲突）：%s", slug)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="创建数字员工失败：数据库约束冲突（详见服务端日志）",
        ) from exc
    session.refresh(agent)
    # ADR-0012：创建即带凭证时自动建专属 Bot 连接（im.enable 关闭时 reload_agent 内部跳过）。
    if payload.im_config:
        _sync_agent_channel_slot(agent)
    _audit_admin(session, request, "create_agent", f"Agent({agent.name})", after=agent)
    session.commit()
    return _agent_to_out(agent)


@router.put("/agents/{agent_id}", response_model=AgentOut)
def update_agent(
    agent_id: int,
    payload: AgentUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> AgentOut:
    """更新数字员工实例（PATCH 语义：仅传入字段生效，name 不可改）。

    - skills/knowledge_scope_tags/allowed_tools/visible_roles 传 [] 表示清空覆盖
      （回到"沿用角色"）；传 None 表示不改。
    - im_config（write-only，按渠道合并）传 None=不改；{}=清空全部渠道；
      {"wecom": {...}}=整体替换该渠道；{"wecom": {}}=移除该渠道。
    """
    agent = _get_agent_or_404(session, agent_id)
    _validate_agent_refs(session, payload)
    # im_config 变更时校验 bot_id 唯一性（None=不改，跳过校验）。
    _validate_agent_im_config(session, agent_id, payload.im_config)
    before = json.dumps(agent.model_dump(), default=str)
    if payload.display_name is not None:
        agent.display_name = payload.display_name
    if payload.description is not None:
        agent.description = payload.description
    if payload.avatar_url is not None:
        agent.avatar_url = payload.avatar_url
    if payload.system_prompt_version is not None:
        agent.system_prompt_version = payload.system_prompt_version
    # 显式允许清零：若 payload 字段存在且为 None，则清为 None（传 null 即清空覆盖）
    # 由于 AgentUpdate 字段默认为 None，需用 model_fields_set 区分"未传" vs "传 null"
    if (
        "system_prompt_version" in payload.model_fields_set
        and payload.system_prompt_version is None
    ):
        agent.system_prompt_version = None
    if "model_id" in payload.model_fields_set:
        agent.model_id = payload.model_id
    if "skills" in payload.model_fields_set:
        agent.skills = payload.skills
    if "knowledge_scope_tags" in payload.model_fields_set:
        agent.knowledge_scope_tags = payload.knowledge_scope_tags
    if "allowed_tools" in payload.model_fields_set:
        agent.allowed_tools = payload.allowed_tools
    if "visible_roles" in payload.model_fields_set:
        agent.visible_roles = payload.visible_roles
    if payload.channel is not None:
        agent.channel = payload.channel
    if payload.im_config is not None:
        # 按渠道合并（2026-09-22 起）：{}=清空全部；渠道分组整体替换/空分组移除；
        # 未提及的渠道不动（区别于旧的「整体替换」语义，多渠道下防误清）。
        from bycall.gateway.channels import (
            agent_channels_from_encrypted,
            merge_im_config,
            parse_im_config_patch,
        )
        from bycall.governance.secrets import encrypt_secret

        current = agent_channels_from_encrypted(agent.im_config_encrypted)
        merged = merge_im_config(current, parse_im_config_patch(payload.im_config))
        agent.im_config_encrypted = (
            encrypt_secret(json.dumps(merged, ensure_ascii=False)) if merged else ""
        )
    if "approval_policy" in payload.model_fields_set:
        agent.approval_policy = payload.approval_policy
    if payload.is_enabled is not None:
        agent.is_enabled = payload.is_enabled
    session.add(agent)
    session.commit()
    session.refresh(agent)
    # ADR-0012：im_config / 启用态 / 删除变化后自动同步专属 Bot 槽位（建连或断开）。
    im_touched = payload.im_config is not None or payload.is_enabled is not None
    if im_touched:
        _sync_agent_channel_slot(agent)
    _audit_admin(
        session,
        request,
        "update_agent",
        f"Agent({agent.name})",
        before=json.loads(before),
        after=agent,
    )
    session.commit()
    return _agent_to_out(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(
    agent_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除数字员工实例。

    若仍有会话引用该实例（Conversation.agent_id），拒绝删除（409），避免悬空历史。
    """
    agent = _get_agent_or_404(session, agent_id)
    # 校验是否有会话引用（历史完整性，避免删后查历史找不到归属）
    ref = session.exec(
        select(Conversation).where(Conversation.agent_id == agent_id).limit(1)
    ).first()
    if ref is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"数字员工 '{agent.name}' 仍有会话引用，无法删除（请先归档或迁移会话）",
        )
    session.delete(agent)
    session.commit()
    # ADR-0012：实例删除后停其专属 Bot 连接（不依赖下一次 reload 清理）。
    from bycall.gateway.manager import get_channel_manager

    try:
        get_channel_manager().stop_agent(agent_id)
    except Exception:
        logger.exception("删除数字员工后停止专属 Bot 槽位失败 agent_id=%s", agent_id)
    _audit_admin(
        session, request, "delete_agent", f"Agent({agent.name})", before={"name": agent.name}
    )


# ============ 数字员工头像（本地图片上传，不使用外链 URL）============


@router.post(
    "/agents/{agent_id}/avatar",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_agent_avatar(
    agent_id: int,
    file: UploadFile,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> AgentOut:
    """上传数字员工头像（本地图片，管理员不再手贴 URL）。

    类型按**文件头魔数**识别（PNG/JPEG/GIF/WebP，拒 SVG），上限 2MB；落盘后把
    ``avatar_url`` 写成带内容哈希的站内读取路径并返回更新后的实例，前端据此直接渲染。
    """
    from bycall.admin import avatars

    agent = _get_agent_or_404(session, agent_id)
    raw = await file.read(avatars.MAX_AVATAR_BYTES + 1)
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="头像文件为空")
    if len(raw) > avatars.MAX_AVATAR_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"头像过大（上限 {avatars.MAX_AVATAR_BYTES // (1024 * 1024)}MB）",
        )
    sniffed = avatars.sniff_image(raw)
    if sniffed is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="仅支持 PNG / JPEG / GIF / WebP 图片"
        )
    ext, _mime = sniffed

    before = agent.avatar_url
    avatars.save_avatar(agent_id, raw, ext)
    agent.avatar_url = avatars.avatar_web_url(agent_id, raw)
    session.add(agent)
    session.commit()
    session.refresh(agent)
    _audit_admin(
        session,
        request,
        "upload_agent_avatar",
        f"Agent({agent.name})",
        before=before,
        after=agent.avatar_url,
    )
    session.commit()
    return _agent_to_out(agent)


@router.get("/agents/{agent_id}/avatar")
def get_agent_avatar(
    agent_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> FileResponse:
    """读取数字员工头像字节（站内路径；存量外链不在此端点服务，前端直接渲染原值）。"""
    from bycall.admin import avatars

    _get_agent_or_404(session, agent_id)
    path = avatars.avatar_path(agent_id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="该数字员工尚未上传头像")
    media_type = avatars.CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    # 缓存靠 avatar_url 的 ?v= 内容哈希失效，长缓存安全。
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
    )


@router.delete("/agents/{agent_id}/avatar", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent_avatar(
    agent_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """移除数字员工头像：清空 ``avatar_url`` 并删除本地文件（存量外链一并清除）。"""
    from bycall.admin import avatars

    agent = _get_agent_or_404(session, agent_id)
    before = agent.avatar_url
    avatars.remove_avatar(agent_id)
    agent.avatar_url = ""
    session.add(agent)
    session.commit()
    if before:
        _audit_admin(session, request, "delete_agent_avatar", f"Agent({agent.name})", before=before)
        session.commit()


# ============ 知识库管理（KnowledgeBase + Document，参考 WeKnora）============


def _get_kb_or_404(session: Session, kb_id: int) -> KnowledgeBase:
    """按 id 取知识库，不存在 → 404。"""
    kb = session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"知识库不存在: {kb_id}",
        )
    return kb


def _get_document_or_404(session: Session, doc_id: int) -> Document:
    """按 id 取文档，不存在 → 404。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档不存在: {doc_id}",
        )
    return doc


def _delete_chunks_for_doc(kb: KnowledgeBase, doc_id: int) -> None:
    """删除某 KB collection 里某文档的所有 chunk。

    用 ``delete_by_filter``（zvec 原生按标量 filter 批删）按 ``doc_id`` 字段删除。
    用 KB 的 collection_name + dimension 建 store。

    ⚠️ 入库时 doc_id 写成 ``f"doc_{doc_id}"``（见 ``_ingest_document`` / ``ingest.ingest_text``），
    删除过滤值必须带上 ``doc_`` 前缀，否则永远删不掉旧 chunk（幽灵 chunk 可被检索）。
    """
    from bycall.config import get_settings
    from bycall.vector_store.factory import create_vector_store
    from bycall.vector_store.models import FilterCondition

    store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
    store.open(kb.collection_name)
    try:
        store.delete_by_filter([FilterCondition(field="doc_id", op="match", value=f"doc_{doc_id}")])
        store.flush()
    finally:
        store.close()


def _kb_chunking_config(kb: KnowledgeBase):
    """从 KB 的切块配置字段构造 ChunkingConfig（入库/预览共用）。"""
    from bycall.rag.chunking import ChunkingConfig

    return ChunkingConfig(
        chunk_size=kb.chunk_size,
        overlap=kb.chunk_overlap,
        separators=list(kb.separators)
        if kb.separators
        else ["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", "，", ",", " "],
        strategy=kb.strategy,
        enable_parent_child=kb.enable_parent_child,
        parent_size=kb.parent_chunk_size,
        child_size=kb.child_chunk_size,
    )


def _ingest_document(
    session: Session,
    kb: KnowledgeBase,
    doc_id: int,
    title: str,
    source: str,
    content: str,
    on_progress=None,
) -> DocumentIngestResult:
    """同步入库：标记 processing → 用 KB 的 embedding 模型 → 删旧 chunk → 入库 → 更新。

    保留同步语义，仅给 ``update_document`` 改 source 这种需要即时回滚的路径用——
    失败立刻返回，调用方可据 result.ok 回滚 ACL。``on_progress`` 把入库阶段回调出去，
    与异步入库共享同一套进度语义。

    全链路结构化日志（修复"入库失败无日志"）：开始/模型解析/删旧/写库/完成或失败
    各阶段都留痕，错误带 kb/doc/title 上下文，便于定位。

    embedding 模型来自 KB 绑定（不是全局激活），维度来自 KB.dimension。
    入库失败 → status=failed + error_message。
    入库成功 → status=completed + chunk_count + 更新 KB 统计。
    """
    from bycall.config import get_settings
    from bycall.llm.resolver import resolve_embed_model
    from bycall.rag.ingest import IngestPipeline
    from bycall.vector_store.factory import create_vector_store

    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不存在: {doc_id}")

    # 1. 标记 processing。
    doc.status = "processing"
    doc.error_message = ""
    session.add(doc)
    session.commit()
    logger.info(
        "入库开始: kb=%s(%s) doc=%s title=%r source=%r 字符数=%d",
        kb.id,
        kb.name,
        doc_id,
        title,
        source,
        len(content),
    )

    # 2. 用 KB 绑定的 embedding 模型。
    embed_entity = session.get(Model, kb.embedding_model_id)
    if embed_entity is None:
        error = f"KB 绑定的 embedding 模型不存在 (id={kb.embedding_model_id})"
        logger.error("入库失败·模型缺失: kb=%s(%s) doc=%s %s", kb.id, kb.name, doc_id, error)
        doc.status = "failed"
        doc.error_message = error
        session.add(doc)
        session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error)

    embed_model = resolve_embed_model(session, embed_entity)

    # 3+4. 删旧 chunk + 入库。同一 collection 串行（守 zvec GC 释放的写锁），
    #      删旧与写库在一个锁段内，避免并发任务互相踩写锁。
    errors: list[str] = []
    chunk_count = 0
    with _get_collection_ingest_lock(kb.collection_name):
        # 删旧 chunk（幂等，支持 reindex）。
        try:
            _delete_chunks_for_doc(kb, doc_id)
        except Exception as exc:
            error = f"删除旧索引失败: {exc}"
            logger.error(
                "入库失败·删旧 chunk 异常: kb=%s(%s) doc=%s %s", kb.id, kb.name, doc_id, error
            )
            doc.status = "failed"
            doc.error_message = error
            session.add(doc)
            session.commit()
            return DocumentIngestResult(
                ok=False,
                document_id=doc_id,
                chunk_count=doc.chunk_count,
                errors=[error],
            )

        # 入库（用 KB 的 collection + 维度）。
        try:
            store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
            try:
                pipeline = IngestPipeline(store, embed_model, collection_name=kb.collection_name)
                config = _kb_chunking_config(kb)
                result = pipeline.ingest_text(
                    content,
                    source=source,
                    doc_id=f"doc_{doc_id}",
                    level=doc.level,
                    config=config,
                    on_progress=on_progress,
                )
                errors = result.errors
                chunk_count = result.chunk_count
            finally:
                store.close()
        except Exception as exc:
            logger.exception("入库异常: kb=%s(%s) doc=%s", kb.id, kb.name, doc_id)
            errors.append(f"入库异常: {exc}")

    # 5. 更新 Document + KB 统计。
    old_chunk_count = doc.chunk_count
    doc.status = "completed" if not errors else "failed"
    doc.chunk_count = chunk_count
    doc.char_count = len(content)
    doc.embedding_model_id = kb.embedding_model_id
    doc.error_message = "; ".join(errors) if errors else ""
    session.add(doc)

    # 更新 KB 的 chunk_count（差值）+ document_count（不变，文档已存在）
    kb.chunk_count += chunk_count - old_chunk_count
    kb.total_chars += len(content)
    session.add(kb)
    session.commit()
    session.refresh(doc)

    if errors:
        ingest_tracker.fail(doc_id, "; ".join(errors))
        logger.error(
            "入库失败: kb=%s(%s) doc=%s 切块=%d 错误=%s",
            kb.id,
            kb.name,
            doc_id,
            chunk_count,
            "; ".join(errors),
        )
    else:
        ingest_tracker.finish(doc_id)
        logger.info(
            "入库完成: kb=%s(%s) doc=%s 切块=%d",
            kb.id,
            kb.name,
            doc_id,
            chunk_count,
        )

    return DocumentIngestResult(
        ok=doc.status == "completed",
        document_id=doc_id,
        chunk_count=chunk_count,
        errors=errors,
    )


def _run_background_ingest(kb_id: int, doc_id: int, title: str, source: str, content: str) -> None:
    """后台入库工作线程：用独立 session 跑完整入库，全程写终态，绝不卡在 processing。

    与 ``_ingest_document`` 同构（模型解析→删旧→入库→更新 DB+KB 统计），差异：
    - 开自己的 ``session_scope()``（线程内独立会话，不碰请求 Session）。
    - 进度通过 ``ingest_tracker`` 写出，供 /progress 端点轮询。
    - 任何异常都兜底写 status=failed + error_message，保证不会留下"processing 僵尸"。
    """
    from bycall.config import get_settings
    from bycall.llm.resolver import resolve_embed_model
    from bycall.rag.ingest import IngestPipeline
    from bycall.vector_store.factory import create_vector_store

    # tracker 已在提交时（请求线程）start 过，这里不再重复；回调/失败会更新它。
    try:
        with session_scope() as session:
            kb = session.get(KnowledgeBase, kb_id)
            doc = session.get(Document, doc_id)
            if kb is None or doc is None:
                # KB 或文档在提交后被删——无法写终态，只更新 tracker 后退出。
                logger.warning("后台入库取消: kb/doc 已不存在 kb=%s doc=%s", kb_id, doc_id)
                ingest_tracker.fail(doc_id, "知识库或文档已被删除")
                return

            logger.info(
                "后台入库开始: kb=%s(%s) doc=%s title=%r source=%r 字符数=%d",
                kb.id,
                kb.name,
                doc_id,
                title,
                source,
                len(content),
            )

            # 1. 用 KB 绑定的 embedding 模型。
            embed_entity = session.get(Model, kb.embedding_model_id)
            if embed_entity is None:
                error = f"KB 绑定的 embedding 模型不存在 (id={kb.embedding_model_id})"
                logger.error(
                    "后台入库失败·模型缺失: kb=%s(%s) doc=%s %s",
                    kb.id,
                    kb.name,
                    doc_id,
                    error,
                )
                _persist_ingest_failure(session, doc, error, doc_id)
                return

            embed_model = resolve_embed_model(session, embed_entity)

            # 2+3. 删旧 + 入库（同一 collection 串行，守 zvec 写锁）。
            errors: list[str] = []
            chunk_count = 0
            with _get_collection_ingest_lock(kb.collection_name):
                try:
                    _delete_chunks_for_doc(kb, doc_id)
                except Exception as exc:
                    error = f"删除旧索引失败: {exc}"
                    logger.error(
                        "后台入库失败·删旧 chunk 异常: kb=%s(%s) doc=%s %s",
                        kb.id,
                        kb.name,
                        doc_id,
                        error,
                    )
                    _persist_ingest_failure(session, doc, error, doc_id)
                    return

                try:
                    store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
                    try:
                        pipeline = IngestPipeline(
                            store, embed_model, collection_name=kb.collection_name
                        )
                        config = _kb_chunking_config(kb)
                        result = pipeline.ingest_text(
                            content,
                            source=source,
                            doc_id=f"doc_{doc_id}",
                            level=doc.level,
                            config=config,
                            on_progress=lambda **kw: ingest_tracker.update(doc_id, **kw),
                        )
                        errors = result.errors
                        chunk_count = result.chunk_count
                    finally:
                        store.close()
                except Exception as exc:
                    logger.exception("后台入库异常: kb=%s(%s) doc=%s", kb.id, kb.name, doc_id)
                    errors.append(f"入库异常: {exc}")

            # 4. 更新 Document + KB 统计（成功/软错误都落终态）。
            success = not errors
            old_chunk_count = doc.chunk_count
            doc.status = "completed" if success else "failed"
            doc.chunk_count = chunk_count
            doc.char_count = len(content)
            doc.embedding_model_id = kb.embedding_model_id
            doc.error_message = "; ".join(errors) if errors else ""
            session.add(doc)
            kb.chunk_count += chunk_count - old_chunk_count
            kb.total_chars += len(content)
            session.add(kb)
            session.commit()

            if success:
                ingest_tracker.finish(doc_id)
                logger.info(
                    "后台入库完成: kb=%s(%s) doc=%s 切块=%d",
                    kb.id,
                    kb.name,
                    doc_id,
                    chunk_count,
                )
            else:
                ingest_tracker.fail(doc_id, "; ".join(errors))
                logger.error(
                    "后台入库失败: kb=%s(%s) doc=%s 切块=%d 错误=%s",
                    kb.id,
                    kb.name,
                    doc_id,
                    chunk_count,
                    "; ".join(errors),
                )
    except Exception:
        # 终极兜底：上面任何未预期异常也要写 failed，绝不静默留 processing。
        logger.exception("后台入库未捕获异常: kb=%s doc=%s", kb_id, doc_id)
        try:
            with session_scope() as session:
                doc = session.get(Document, doc_id)
                if doc is not None:
                    doc.status = "failed"
                    doc.error_message = "入库未捕获异常，请联系管理员或重试"
                    session.add(doc)
                    session.commit()
        except Exception:
            logger.exception("写入失败终态时再次异常: doc=%s", doc_id)
        ingest_tracker.fail(doc_id, "入库未捕获异常")


def _persist_ingest_failure(session: Session, doc: Document, error: str, doc_id: int) -> None:
    """后台入库中途失败：写 status=failed + error_message 并提交，同步更新 tracker。"""
    doc.status = "failed"
    doc.error_message = error
    session.add(doc)
    session.commit()
    ingest_tracker.fail(doc_id, error)


def _run_background_parse_and_ingest(
    kb_id: int,
    doc_id: int,
    title: str | None,
    source: str,
    file_name: str,
    raw: bytes,
    engine: str | None,
) -> None:
    """后台解析 + 入库工作线程：先解析二进制（可能很慢，如 mineru），再复用入库体。

    解析在 collection 入库锁**之外**——多文件并发上传时，各文件的解析互不阻塞
    （仅最终写库阶段按 collection 串行，守 zvec 写锁）。参考 WeKnora：上传立即受理，
    解析/入库全程异步，进度经 ``ingest_tracker`` 暴露。

    解析失败 / 空内容 → 写 failed 终态，不进入库阶段。
    """
    from bycall.rag.parsers import parse_document

    # ---- 1. 解析（stage=parsing，慢引擎在此耗时）----
    ingest_tracker.update(doc_id, stage="parsing")
    try:
        result = parse_document(raw, file_name=file_name, engine=engine)
    except ValueError as exc:
        _persist_parse_failure(doc_id, f"文档解析失败: {exc}")
        return
    except Exception as exc:
        logger.exception("后台解析未捕获异常: kb=%s doc=%s", kb_id, doc_id)
        _persist_parse_failure(doc_id, f"文档解析未捕获异常: {exc}")
        return

    content = result.text.strip()
    if not content:
        _persist_parse_failure(doc_id, "文档解析后内容为空")
        return

    # ---- 2. 持久化解析结果（content；请求未给标题时用解析元数据标题兜底）----
    final_title = title or result.title or file_name
    try:
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                ingest_tracker.fail(doc_id, "文档已被删除")
                return
            doc.content = content
            if not title and result.title:
                doc.title = result.title
            session.add(doc)
            session.commit()
    except Exception:
        logger.exception("持久化解析结果失败: kb=%s doc=%s", kb_id, doc_id)
        _persist_parse_failure(doc_id, "持久化解析结果失败")
        return

    # ---- 3. 复用既有入库体（切块→向量化→写库 + 终态 + tracker 兜底）----
    _run_background_ingest(kb_id, doc_id, final_title, source, content)


def _persist_parse_failure(doc_id: int, error: str) -> None:
    """后台解析失败：开独立 session 写 failed 终态 + 更新 tracker（不卡在 processing）。"""
    try:
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                ingest_tracker.fail(doc_id, error)
                return
            doc.status = "failed"
            doc.error_message = error
            session.add(doc)
            session.commit()
    except Exception:
        logger.exception("写入解析失败终态时再次异常: doc=%s", doc_id)
    ingest_tracker.fail(doc_id, error)


def _submit_ingest(kb_id: int, doc_id: int, title: str, source: str, content: str) -> None:
    """把一次入库任务提交到后台线程池（立即返回，不阻塞 HTTP 请求）。

    Future 登记到 ``_ingest_futures``，供测试用 ``_drain_ingest`` 排空，避免跨用例
    残留线程写旧 engine/污染 tracker。运行时不读这个列表。
    """
    # 先在请求线程登记进度（start），让 /progress 在响应返回那一刻就有记录——
    # 否则工作线程调度间隙会误触发"进度不可用（服务可能重启）"提示。
    ingest_tracker.start(doc_id, title)
    future = _get_ingest_executor().submit(
        _run_background_ingest, kb_id, doc_id, title, source, content
    )
    _ingest_futures.append(future)


def _submit_parse_and_ingest(
    kb_id: int,
    doc_id: int,
    title: str | None,
    source: str,
    file_name: str,
    raw: bytes,
    engine: str | None,
) -> None:
    """提交后台解析 + 入库任务（立即返回，不阻塞 HTTP 请求）。

    多文件并发上传 → 多个任务进同一线程池**并行**解析 + 入库，彼此不阻塞
    （参考 WeKnora）。Future 登记到 ``_ingest_futures``，供 ``_drain_ingest`` 排空。
    """
    ingest_tracker.start(doc_id, title or file_name)
    # 立即置 parsing 阶段：让 /progress 在响应返回那一刻就显示"解析中"，
    # 而不是 stage="" 被误判为"进度不可用（服务可能重启）"。
    ingest_tracker.update(doc_id, stage="parsing")
    future = _get_ingest_executor().submit(
        _run_background_parse_and_ingest,
        kb_id,
        doc_id,
        title,
        source,
        file_name,
        raw,
        engine,
    )
    _ingest_futures.append(future)


def _drain_ingest(timeout: float = 30.0) -> None:
    """等待所有已提交的后台入库任务完成（主要用于测试隔离）。

    运行时一般不调用——入库是 fire-and-forget；测试用它确保用例结束前无残留线程。
    """
    from concurrent.futures import wait

    futures = list(_ingest_futures)
    _ingest_futures.clear()
    if futures:
        _done, not_done = wait(futures, timeout=timeout)
        if not_done:
            logger.warning("排空后台入库任务超时: %d 个未完成", len(not_done))


# ============ 知识库 CRUD ============


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseOut,
    status_code=status.HTTP_201_CREATED,
)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> KnowledgeBase:
    """创建知识库：选 embedding 模型 → 读维度 → 建 zvec collection。

    collection_name = f"kb_{id}"，维度来自 embedding 模型的 embed_dimension。
    name 唯一，冲突 → 409。
    """
    # 校验 embedding 模型存在且是 embedding 类型
    embed_model = session.get(Model, payload.embedding_model_id)
    if embed_model is None or embed_model.model_type != "embedding":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"embedding 模型不存在或类型不对: {payload.embedding_model_id}",
        )
    if not embed_model.embed_dimension or embed_model.embed_dimension <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"embedding 模型未设置维度: {embed_model.name}（embed_dimension={embed_model.embed_dimension}）",
        )

    # name 唯一预检
    existing = session.exec(KnowledgeBase.select_by_name(payload.name)).first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"知识库名已存在: {payload.name}",
        )

    # 先建 KB 记录拿到 id（collection_name 需要 id）
    kb = KnowledgeBase(
        name=payload.name,
        description=payload.description,
        embedding_model_id=payload.embedding_model_id,
        dimension=embed_model.embed_dimension,
        collection_name="placeholder",  # 先占位，拿到 id 后更新
        visible_to_roles=payload.visible_to_roles,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
        separators=payload.separators
        if payload.separators
        else ["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", "，", ",", " "],
        strategy=payload.strategy,
        enable_parent_child=payload.enable_parent_child,
        parent_chunk_size=payload.parent_chunk_size,
        child_chunk_size=payload.child_chunk_size,
    )
    session.add(kb)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"知识库名已存在: {payload.name}",
        ) from exc
    session.refresh(kb)

    # 更新 collection_name
    kb.collection_name = f"kb_{kb.id}"
    session.add(kb)
    session.commit()
    session.refresh(kb)

    # 建 zvec collection（用 KB 的维度）
    from bycall.config import get_settings
    from bycall.vector_store.factory import create_vector_store

    try:
        store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
        store.open(kb.collection_name)  # 不存在则自动按默认 schema 创建
        store.close()
    except Exception as exc:
        # collection 创建失败 → 删 KB 记录回滚
        session.delete(kb)
        session.commit()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建向量库 collection 失败: {exc}",
        ) from exc

    return kb


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(
    role: str | None = Query(default=None, description="按角色过滤可见 KB"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[KnowledgeBase]:
    """列出知识库。可选按角色过滤：只返回该角色可见的 KB（visible_to_roles 含该角色或为空）。"""
    kbs = list(session.exec(KnowledgeBase.select_all()).all())
    if role:
        kbs = [
            kb
            for kb in kbs
            if kb.visible_to_roles is None
            or len(kb.visible_to_roles) == 0
            or role in kb.visible_to_roles
        ]
    return kbs


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
def get_knowledge_base(
    kb_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> KnowledgeBase:
    """知识库详情。不存在 → 404。"""
    return _get_kb_or_404(session, kb_id)


@router.put("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
def update_knowledge_base(
    kb_id: int,
    payload: KnowledgeBaseUpdate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> KnowledgeBase:
    """更新知识库名称/描述/可见角色列表。不改 embedding 模型（维度锁定）。"""
    kb = _get_kb_or_404(session, kb_id)
    if payload.name is not None and payload.name != kb.name:
        existing = session.exec(KnowledgeBase.select_by_name(payload.name)).first()
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"知识库名已存在: {payload.name}",
            )
        kb.name = payload.name
    if payload.description is not None:
        kb.description = payload.description
    if payload.visible_to_roles is not None:
        kb.visible_to_roles = payload.visible_to_roles
    if payload.chunk_size is not None:
        kb.chunk_size = payload.chunk_size
    if payload.chunk_overlap is not None:
        kb.chunk_overlap = payload.chunk_overlap
    if payload.separators is not None:
        kb.separators = payload.separators
    if payload.strategy is not None:
        kb.strategy = payload.strategy
    if payload.enable_parent_child is not None:
        kb.enable_parent_child = payload.enable_parent_child
    if payload.parent_chunk_size is not None:
        kb.parent_chunk_size = payload.parent_chunk_size
    if payload.child_chunk_size is not None:
        kb.child_chunk_size = payload.child_chunk_size
    if payload.parse_engine_map is not None:
        kb.parse_engine_map = payload.parse_engine_map
    session.add(kb)
    session.commit()
    session.refresh(kb)
    return kb


@router.delete("/knowledge-bases/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_base(
    kb_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除知识库：级联删 collection + 全部文档。

    1. 删 zvec collection（destroy）
    2. 删全部 Document 记录
    3. 删 KB 记录
    """
    from bycall.config import get_settings
    from bycall.vector_store.factory import create_vector_store

    kb = _get_kb_or_404(session, kb_id)

    # 向量数据删除失败时必须保留 DB 元数据，避免返回成功却遗留不可治理的数据。
    try:
        store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
        store.destroy(kb.collection_name)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"删除知识库向量数据失败，元数据已保留: {exc}",
        ) from exc

    # 删全部文档
    docs = session.exec(Document.select_by_kb(kb_id)).all()
    for doc in docs:
        session.delete(doc)

    # 审计（在删 KB 记录前记录）
    _audit_admin(session, request, "delete_knowledge_base", f"KB({kb.name})", before=kb)

    # 删 KB
    session.delete(kb)
    session.commit()


# ============ 文档管理（绑定到 KB）============


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
def list_documents(
    kb_id: int,
    source: str | None = Query(default=None, description="按知识域标签过滤"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[Document]:
    """列出 KB 内文档。可选 source 过滤。"""
    _get_kb_or_404(session, kb_id)
    if source is not None:
        rows = session.exec(
            select(Document).where(Document.knowledge_base_id == kb_id, Document.source == source)
        ).all()
    else:
        rows = session.exec(Document.select_by_kb(kb_id)).all()
    return list(rows)


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=IngestAccepted,
    status_code=status.HTTP_201_CREATED,
)
def create_document(
    kb_id: int,
    payload: DocumentCreate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> IngestAccepted:
    """在指定 KB 内创建文档并提交异步入库。

    校验（空正文/空 source）同步返回 4xx；通过后建文档、置 processing、提交后台入库，
    立即返回 IngestAccepted。前端轮询 /progress 看实时阶段与终态。
    """
    kb = _get_kb_or_404(session, kb_id)

    content = payload.content.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文档正文不能为空")
    if not payload.source.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="知识域标签不能为空")

    file_type = ""
    if payload.file_name:
        ext = payload.file_name.rsplit(".", 1)[-1].lower() if "." in payload.file_name else ""
        file_type = ext if ext in ("txt", "md", "markdown") else ""

    doc = Document(
        knowledge_base_id=kb_id,
        title=payload.title,
        source=payload.source.strip(),
        level=payload.level,
        content=content,
        file_name=payload.file_name,
        file_type=file_type,
        status="processing",
    )
    session.add(doc)
    kb.document_count += 1
    session.add(kb)
    session.commit()
    session.refresh(doc)

    _submit_ingest(kb_id, doc.id, doc.title, doc.source, content)
    return IngestAccepted(document_id=doc.id, status="processing")


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}", response_model=DocumentOut)
def get_document(
    kb_id: int,
    doc_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Document:
    """按 id 取文档详情。"""
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")
    return doc


@router.put("/knowledge-bases/{kb_id}/documents/{doc_id}", response_model=DocumentOut)
def update_document(
    kb_id: int,
    doc_id: int,
    payload: DocumentUpdate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Document:
    """更新文档标题/source/level。改 source 或 level 会重新入库。"""
    kb = _get_kb_or_404(session, kb_id)
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")

    need_reingest = False
    old_source = doc.source
    old_level = doc.level
    if payload.title is not None:
        doc.title = payload.title
    if payload.source is not None and payload.source != doc.source:
        new_source = payload.source.strip()
        if not new_source:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="知识域标签不能为空")
        doc.source = new_source
        need_reingest = True
    if payload.level is not None and payload.level != doc.level:
        doc.level = payload.level
        need_reingest = True

    session.add(doc)
    session.commit()
    session.refresh(doc)

    if need_reingest:
        result = _ingest_document(
            session,
            kb,
            doc.id,
            doc.title,
            doc.source,
            doc.content,
            on_progress=lambda **kw: ingest_tracker.update(doc.id, **kw),
        )
        if not result.ok:
            # 旧 chunk 若仍存在，其 ACL 仍是 old_source/old_level；DB 必须回滚到相同授权状态。
            doc.source = old_source
            doc.level = old_level
            session.add(doc)
            session.commit()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="文档 ACL 更新失败，source 已回滚: " + "; ".join(result.errors),
            )

    session.refresh(doc)
    return doc


@router.delete(
    "/knowledge-bases/{kb_id}/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_document(
    kb_id: int,
    doc_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除文档：级联清理 zvec chunks + DB 记录。"""
    kb = _get_kb_or_404(session, kb_id)
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")

    try:
        _delete_chunks_for_doc(kb, doc_id)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"删除文档向量数据失败，元数据已保留: {exc}",
        ) from exc

    # 审计（在删记录前记录）
    _audit_admin(session, request, "delete_document", f"Doc({doc.title}, kb={kb.name})", before=doc)

    # 更新 KB 统计
    kb.chunk_count -= doc.chunk_count
    kb.document_count -= 1
    session.add(kb)

    session.delete(doc)
    session.commit()


@router.post(
    "/knowledge-bases/{kb_id}/documents/{doc_id}/reindex",
    response_model=IngestAccepted,
)
def reindex_document(
    kb_id: int,
    doc_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> IngestAccepted:
    """重新入库（异步）：置 processing 后提交后台任务，立即返回 IngestAccepted。"""
    _get_kb_or_404(session, kb_id)
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")

    doc.status = "processing"
    doc.error_message = ""
    session.add(doc)
    session.commit()
    session.refresh(doc)

    _submit_ingest(kb_id, doc.id, doc.title, doc.source, doc.content)
    return IngestAccepted(document_id=doc.id, status="processing")


@router.get(
    "/knowledge-bases/{kb_id}/documents/{doc_id}/progress",
    response_model=IngestProgressOut,
)
def get_document_progress(
    kb_id: int,
    doc_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> IngestProgressOut:
    """入库进度：DB 持久态（status/chunk_count/error_message/title）+
    内存 tracker 实时阶段（stage/percent/total/processed）合并。

    - tracker 有记录 → 叠加 stage/percent/total_chunks/processed_chunks。
    - 服务重启导致 processing 文档无 tracker 记录 → stage 置空并给"进度不可用，可重建"
      提示（不假装有进度），前端可据此引导 reindex。
    - DB 已 completed/failed 但 tracker 记录已清 → 从 DB 终态反推 stage/percent。
    """
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")

    live = ingest_tracker.get(doc_id)
    error_message = doc.error_message
    if live is not None:
        stage = live.stage
        percent = live.percent
        total_chunks = live.total_chunks
        processed_chunks = live.processed_chunks
    else:
        # tracker 无记录：从 DB 终态反推阶段（重启/清理后的兜底）。
        total_chunks = 0
        processed_chunks = 0
        if doc.status == "completed":
            stage, percent = "done", 100
        elif doc.status == "failed":
            stage, percent = "failed", 0
        else:
            # processing 但无进度（服务重启中途）→ 明确提示重建。
            stage, percent = "", 0
            if not error_message:
                error_message = "入库进度不可用（服务可能重启），可尝试重建"

    return IngestProgressOut(
        document_id=doc_id,
        title=doc.title,
        status=doc.status,
        stage=stage,
        percent=percent,
        total_chunks=total_chunks,
        processed_chunks=processed_chunks,
        chunk_count=doc.chunk_count,
        error_message=error_message,
    )


@router.get("/ingest/active", response_model=list[IngestProgressOut])
def list_active_ingest(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[IngestProgressOut]:
    """列出所有"进行中"的入库任务（tracker.active() 叠加 DB 持久态）。"""
    out: list[IngestProgressOut] = []
    for live in ingest_tracker.active():
        doc = session.get(Document, live.document_id)
        if doc is None:
            continue
        out.append(
            IngestProgressOut(
                document_id=live.document_id,
                title=doc.title,
                status=doc.status,
                stage=live.stage,
                percent=live.percent,
                total_chunks=live.total_chunks,
                processed_chunks=live.processed_chunks,
                chunk_count=doc.chunk_count,
                error_message=doc.error_message,
            )
        )
    return out


@router.get("/knowledge/sources", response_model=list[str])
def list_knowledge_sources(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[str]:
    """列出所有已用过的 source 标签。"""
    rows = session.exec(Document.select_distinct_sources()).all()
    return sorted(str(r) for r in rows if r)


@router.post(
    "/knowledge-bases/{kb_id}/upload",
    response_model=IngestAccepted,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: int,
    file: UploadFile,
    source: str = Query(..., description="知识域标签"),
    level: int = Query(default=0, ge=0, description="密级/职级（ABAC）：越大越机密"),
    title: str | None = Query(default=None, description="文档标题（默认用文件名）"),
    engine: str | None = Query(
        default=None,
        description="解析引擎：builtin / markitdown / mineru（默认用配置 BYCALL_PARSE_ENGINE）",
    ),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> IngestAccepted:
    """文件上传入库（到指定 KB）——上传立即受理，解析与入库全程异步（参考 WeKnora）。

    支持格式：PDF / Word / Markdown / HTML / TXT / CSV / JSON / 代码等（随解析引擎扩展，
    选 markitdown/mineru 时额外支持 pptx/xlsx/图片等）。
    请求线程只做廉价同步校验（扩展名 / 引擎可用性 / 大小），**解析在后台线程**——
    多文件并发上传互不阻塞（慢引擎如 mineru 不会卡住其他文件）。
    解析/空内容/解析异常不再立即 4xx，而是在后台落 status=failed，经 /progress 暴露。

    解析引擎可切换（``engine`` 参数覆盖配置默认）；引擎不支持该类型时自动退回 builtin。
    """
    kb = _get_kb_or_404(session, kb_id)

    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件名不能为空")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "txt"

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB）",
        )

    # 廉价同步校验：扩展名 + 引擎可用性（不触发解析）。引擎不可用属配置错误，立即 4xx。
    # 引擎解析优先级：上传显式 engine > KB 的 parse_engine_map[ext] > map["default"] > 全局默认。
    from bycall.rag.parsers import get_parse_engine, resolve_engine_name, supported_upload_types

    if ext not in supported_upload_types():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: .{ext}",
        )

    effective_engine = resolve_engine_name(ext, engine=engine, engine_map=kb.parse_engine_map or {})
    if effective_engine != "builtin":
        eng = get_parse_engine(effective_engine)
        if not eng.is_available():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"解析引擎 {effective_engine!r} 不可用：{eng.reason_unavailable}",
            )

    # 建文档（content 暂空——解析在后台完成后再回填），置 processing，立即提交后台解析+入库。
    doc = Document(
        knowledge_base_id=kb_id,
        title=title or file.filename.rsplit(".", 1)[0],
        source=source.strip(),
        level=level,
        content="",
        file_name=file.filename,
        file_type=ext,
        status="processing",
    )
    session.add(doc)
    kb.document_count += 1
    session.add(kb)
    session.commit()
    session.refresh(doc)

    # 解析 + 入库都在后台：上传立即返回，多文件并发互不阻塞。
    # 传已解析的引擎名（含 KB 映射），后台 parse_document 收到显式 engine 即直接采用。
    _submit_parse_and_ingest(kb_id, doc.id, title, doc.source, file.filename, raw, effective_engine)
    return IngestAccepted(document_id=doc.id, status="processing")


@router.post(
    "/knowledge-bases/{kb_id}/documents/preview-chunks",
    response_model=ChunkPreviewResult,
)
def preview_chunks(
    kb_id: int,
    payload: ChunkPreviewRequest,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ChunkPreviewResult:
    """切块预览——仅运行切块逻辑，不 embedding、不写库（参考 WeKnora chunker/preview）。

    用入库时同样的默认 ``ChunkingConfig``（可选覆盖 chunk_size/overlap/父子块），
    让管理员在入库前后验证/调参切块效果，避免"入了库才发现切得不对"。
    """
    kb = _get_kb_or_404(session, kb_id)

    from bycall.rag.chunking import Document, documents_to_pre_chunks

    config = _kb_chunking_config(kb)
    # 允许请求参数覆盖 KB 配置（调参预览）
    if payload.chunk_size is not None:
        config.chunk_size = payload.chunk_size
    if payload.overlap is not None:
        config.overlap = payload.overlap
    if payload.strategy is not None:
        config.strategy = payload.strategy
    if payload.enable_parent_child is not None:
        config.enable_parent_child = payload.enable_parent_child
    doc = Document(doc_id="preview", text=payload.text, source="preview", title=payload.title)
    pre_chunks = documents_to_pre_chunks([doc], config=config)

    items = [
        ChunkPreviewItem(
            id=p.id,
            chunk_index=p.chunk_index,
            chunk_type=p.chunk_type,
            char_count=len(p.text),
            text=p.text,
            parent_id=p.parent_id,
        )
        for p in pre_chunks
    ]
    return ChunkPreviewResult(
        total=len(items),
        child_count=sum(1 for i in items if i.chunk_type == "child"),
        parent_count=sum(1 for i in items if i.chunk_type == "parent"),
        items=items,
    )


@router.get(
    "/knowledge-bases/{kb_id}/documents/{doc_id}/content",
    response_model=DocumentContentOut,
)
def get_document_content(
    kb_id: int,
    doc_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> DocumentContentOut:
    """读取文档原始正文。

    ``DocumentOut`` 刻意不带 content（可能很长），调试/核对入库内容时用此端点单独取。
    """
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")
    return DocumentContentOut.model_validate(doc)


# ============ Chunk 列表（调试/排查检索效果用）============


@router.get("/knowledge-bases/{kb_id}/chunks", response_model=KBChunkListResult)
def list_kb_chunks(
    kb_id: int,
    doc_id: str | None = Query(default=None, description="按 doc_id 过滤（如 doc_42）"),
    limit: int = Query(default=50, ge=1, le=500, description="返回条数"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> KBChunkListResult:
    """列出 KB 内的 chunk 元数据（按 doc_id 过滤，用于调试检索效果）。"""
    kb = _get_kb_or_404(session, kb_id)

    from bycall.config import get_settings
    from bycall.vector_store.factory import create_vector_store
    from bycall.vector_store.models import FilterCondition, SearchRequest

    store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
    try:
        store.open(kb.collection_name)
        filters = []
        if doc_id:
            filters.append(FilterCondition(field="doc_id", op="=", value=doc_id))
        dummy_vec = [0.0] * kb.dimension
        req = SearchRequest(
            vector=dummy_vec,
            filters=filters if filters else None,
            topk=limit,
            output_fields=[
                "text",
                "chunk_type",
                "chunk_index",
                "doc_id",
                "source",
                "title",
                "parent_id",
            ],
        )
        results = store.search(req)
    finally:
        store.close()

    items = [
        KBChunkOut(
            chunk_id=r.id,
            chunk_type=str(r.fields.get("chunk_type", "child")),
            chunk_index=int(r.fields.get("chunk_index", 0)),
            doc_id=str(r.fields.get("doc_id", "")),
            source=str(r.fields.get("source", "")),
            title=str(r.fields.get("title", "")),
            text=r.text[:500] if r.text else "",
            char_count=len(r.text) if r.text else 0,
            parent_id=str(r.fields.get("parent_id", "")) or None,
        )
        for r in results
    ]
    return KBChunkListResult(kb_id=kb_id, doc_id=doc_id, total=len(items), items=items)


# ============ KB 检索（admin 调试用）============


@router.post("/knowledge-bases/{kb_id}/search", response_model=KBSearchResult)
def search_kb(
    kb_id: int,
    payload: KBSearchRequest,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> KBSearchResult:
    """在指定 KB 内执行混合检索，用于 admin 验证检索效果。"""
    kb = _get_kb_or_404(session, kb_id)

    from bycall.config import get_settings
    from bycall.rag.retrieve import retrieve_multi_kb

    tags = payload.knowledge_scope_tags
    results = retrieve_multi_kb(
        get_settings().vector_store,
        kb_specs=[(kb.id, kb.collection_name, kb.dimension, kb.embedding_model_id)],
        question=payload.question,
        knowledge_scope_tags=tags,
        topk=payload.topk,
        session=session,
    )

    items = [
        KBSearchResultItem(
            chunk_id=r.id,
            score=round(r.score, 4),
            text=r.text[:300] if r.text else "",
            chunk_type=str(r.fields.get("chunk_type", "child")),
            doc_id=str(r.fields.get("doc_id", "")),
            source=str(r.fields.get("source", "")),
            title=str(r.fields.get("title", "")),
        )
        for r in results
    ]
    return KBSearchResult(kb_id=kb_id, question=payload.question, results=items, total=len(items))


@router.get(
    "/knowledge-bases/{kb_id}/documents/{doc_id}/chunks",
    response_model=KBChunkListResult,
)
def list_document_chunks(
    kb_id: int,
    doc_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> KBChunkListResult:
    """列出指定文档的所有切块。"""
    _get_document_or_404(session, doc_id)
    from bycall.config import get_settings
    from bycall.vector_store.factory import create_vector_store
    from bycall.vector_store.models import FilterCondition, SearchRequest

    kb = _get_kb_or_404(session, kb_id)
    store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
    try:
        store.open(kb.collection_name)
        dummy_vec = [0.0] * kb.dimension
        doc_id_str = f"doc_{doc_id}"
        req = SearchRequest(
            vector=dummy_vec,
            filters=[FilterCondition(field="doc_id", op="=", value=doc_id_str)],
            topk=limit,
            output_fields=[
                "text",
                "chunk_type",
                "chunk_index",
                "doc_id",
                "source",
                "title",
                "parent_id",
            ],
        )
        results = store.search(req)
    finally:
        store.close()

    items = [
        KBChunkOut(
            chunk_id=r.id,
            chunk_type=str(r.fields.get("chunk_type", "child")),
            chunk_index=int(r.fields.get("chunk_index", 0)),
            doc_id=str(r.fields.get("doc_id", "")),
            source=str(r.fields.get("source", "")),
            title=str(r.fields.get("title", "")),
            text=r.text[:500] if r.text else "",
            char_count=len(r.text) if r.text else 0,
            parent_id=str(r.fields.get("parent_id", "")) or None,
        )
        for r in results
    ]
    return KBChunkListResult(kb_id=kb_id, doc_id=str(doc_id), total=len(items), items=items)


@router.post(
    "/knowledge-bases/{kb_id}/documents/batch-reindex",
    response_model=list[IngestAccepted],
)
def batch_reindex_documents(
    kb_id: int,
    payload: BatchReindexRequest,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[IngestAccepted]:
    """批量重新入库（异步）：每个文档置 processing 后各自提交后台入库，返回受理列表。

    设计选择：批量场景本质是"提交一堆异步入库"，旧的 BatchReindexResult
    （succeeded/failed/details）是同步语义——任务还没跑完时给"成功/失败"计数会误导。
    改为返回 ``list[IngestAccepted]``：每个受理的文档一个条目，前端按 document_id
    各自轮询 /progress。文档不存在/不属于本 KB 同步校验直接 404（fail-fast，避免
    静默丢弃），与 create/upload 的同步校验语义一致。
    """
    _get_kb_or_404(session, kb_id)

    # 先同步校验全部 doc_id 归属，任一无效直接 404（不静默丢弃）。
    accepted: list[IngestAccepted] = []
    docs: list[Document] = []
    for doc_id in payload.document_ids:
        doc = session.get(Document, doc_id)
        if doc is None or doc.knowledge_base_id != kb_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"文档不存在或不属于该知识库: {doc_id}",
            )
        docs.append(doc)

    # 全部合法 → 置 processing、提交后台入库。
    for doc in docs:
        doc.status = "processing"
        doc.error_message = ""
        session.add(doc)
    session.commit()
    for doc in docs:
        _submit_ingest(kb_id, doc.id, doc.title, doc.source, doc.content)
        accepted.append(IngestAccepted(document_id=doc.id, status="processing"))
    return accepted


# ============ 会话管理（Conversation + Message，参考 Hermes SessionStore）============


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    user_id: int | None = Query(default=None, description="按用户过滤"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[Conversation]:
    """列出会话。可选 user_id 过滤。按最后消息时间倒序。"""
    if user_id is not None:
        rows = session.exec(Conversation.select_by_user(user_id)).all()
    else:
        rows = session.exec(Conversation.select_all()).all()
    return list(rows)


@router.get("/conversations/{conv_id}", response_model=ConversationOut)
def get_conversation(
    conv_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Conversation:
    """会话详情。不存在 → 404。"""
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"会话不存在: {conv_id}")
    return conv


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
def list_messages(
    conv_id: int,
    limit: int = Query(default=50, ge=1, le=500, description="返回最近 N 条消息"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[Message]:
    """查看会话消息历史。按时间正序返回（旧→新）。"""
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"会话不存在: {conv_id}")
    rows = session.exec(Message.select_recent(conv_id, limit=limit)).all()
    return list(reversed(rows))


@router.delete("/conversations/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conv_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除会话：删全部消息 + 会话记录（物理删除，不可恢复）。

    审计日志不受影响（AuditLog 独立于 Conversation/Message）。
    """
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"会话不存在: {conv_id}")

    msgs = session.exec(Message.select_by_conversation(conv_id)).all()
    for msg in msgs:
        session.delete(msg)
    session.delete(conv)
    session.commit()


@router.post("/conversations/{conv_id}/reset", response_model=ConversationOut)
def reset_conversation(
    conv_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Conversation:
    """重置会话：清全部消息 + 清 summary，但保留会话本身。

    与删除的区别：会话记录还在，session_key 不变，只是历史清空。
    """
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"会话不存在: {conv_id}")

    msgs = session.exec(Message.select_by_conversation(conv_id)).all()
    for msg in msgs:
        session.delete(msg)
    conv.summary = ""
    conv.message_count = 0
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


# ============ 用户长期记忆（UserMemory）============


@router.get("/users/{user_id}/memory", response_model=list[MemoryOut])
def list_user_memory(
    user_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[UserMemory]:
    """列出用户全部长期记忆。"""
    return list(session.exec(UserMemory.select_by_user(user_id)).all())


@router.post(
    "/users/{user_id}/memory",
    response_model=MemoryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_user_memory(
    user_id: int,
    payload: MemoryCreate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> UserMemory:
    """添加用户记忆条目。category ∈ {preference, fact, note}。"""
    if payload.category not in ("preference", "fact", "note"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="category 必须是 preference / fact / note 之一",
        )
    mem = UserMemory(
        user_id=user_id,
        category=payload.category,
        content=payload.content.strip(),
    )
    session.add(mem)
    session.commit()
    session.refresh(mem)
    return mem


@router.put("/memory/{mem_id}", response_model=MemoryOut)
def update_user_memory(
    mem_id: int,
    payload: MemoryUpdate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> UserMemory:
    """更新记忆条目（PATCH 语义）。不存在 → 404。"""
    mem = session.get(UserMemory, mem_id)
    if mem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"记忆条目不存在: {mem_id}")
    if payload.category is not None:
        if payload.category not in ("preference", "fact", "note"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="category 必须是 preference / fact / note 之一",
            )
        mem.category = payload.category
    if payload.content is not None:
        mem.content = payload.content.strip()
    # S2：admin 可标记记忆为 superseded（已纠正/作废，不再注入）或恢复 active。
    if payload.status is not None:
        if payload.status not in ("active", "superseded"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="status 必须是 active / superseded 之一",
            )
        mem.status = payload.status
    session.add(mem)
    session.commit()
    session.refresh(mem)
    return mem


@router.delete("/memory/{mem_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_memory(
    mem_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除记忆条目。不存在 → 404。"""
    mem = session.get(UserMemory, mem_id)
    if mem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"记忆条目不存在: {mem_id}")
    session.delete(mem)
    session.commit()


# ============ 工具目录（M3 工具治理）============


@router.get("/tools", response_model=list[ToolOut])
def list_tools(
    _admin: None = Depends(_require_admin),
) -> list[ToolOut]:
    """列出已注册的工具目录（只读）。

    工具不是 DB 实体（实现是代码，见 ``tools/registry.py``），本端点返回进程级注册表里
    的全部工具，供 admin 在角色编辑时渲染 ``allowed_tools`` 多选框。

    ``allowed_tools`` 的实际增删仍走 ``PATCH /api/admin/roles/{name}``（RoleConfig 字段）。
    """
    from bycall.tools.registry import get_all_tools

    return [
        ToolOut(name=t.name, description=t.description, parameters=t.parameters, source=t.source)
        for t in get_all_tools()
    ]


# ============ 解析引擎（可切换：builtin / markitdown / mineru）============


@router.get("/parse-engines", response_model=list[dict])
def list_parse_engines_endpoint(
    _admin: None = Depends(_require_admin),
) -> list[dict]:
    """列出可选的解析引擎及其可用性、支持的文件类型。

    引擎在按扩展名路由的解析器（``/parsers``）之上：选 markitdown/mineru 时，
    其声明支持的类型走该引擎，其余类型仍退回 builtin。返回每个引擎的
    ``is_available``（依赖/远程是否就绪）便于前端提示配置缺失。
    """
    from bycall.rag.parsers import list_parse_engines

    return list_parse_engines()


# ============ 解析器注册表（文档解析器列表）============


@router.get("/parsers", response_model=list[dict])
def list_parsers_endpoint(
    _admin: None = Depends(_require_admin),
) -> list[dict]:
    """列出已注册的文档解析器及其支持的文件类型。"""
    from bycall.rag.parsers.registry import list_parsers

    return list_parsers()


# ============ MCP 工具服务器（M4 工具网关）============


def _get_mcp_or_404(session: Session, server_id: int):
    """按 id 取 MCP server，不存在 → 404。"""
    from bycall.governance.models import MCPServer

    srv = session.get(MCPServer, server_id)
    if srv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server 不存在: {server_id}",
        )
    return srv


def _mcp_to_out(srv) -> MCPServerOut:
    """MCPServer ORM → MCPServerOut。env/headers 绝不回显，只标 has_*。

    tool_count/connected 从 MCPManager 单例状态取（反映运行时连接，非 DB 持久态）。
    """
    from bycall.mcp.manager import get_mcp_manager

    tool_count, connected = 0, False
    for st in get_mcp_manager().status():
        if st["name"] == srv.name:
            tool_count, connected = st["tool_count"], st["connected"]
    args = json.loads(srv.args_json) if srv.args_json else []
    return MCPServerOut(
        id=srv.id,
        name=srv.name,
        description=srv.description,
        transport=srv.transport,
        command=srv.command,
        args=args,
        url=srv.url,
        is_enabled=srv.is_enabled,
        has_env=bool(srv.env_encrypted),
        has_headers=bool(srv.headers_encrypted),
        tool_count=tool_count,
        connected=connected,
        created_at=srv.created_at,
        updated_at=srv.updated_at,
    )


@router.get("/mcp-servers", response_model=list[MCPServerOut])
def list_mcp_servers(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[MCPServerOut]:
    """列出全部 MCP server（按 id 升序）。env/headers 不回显。"""
    from bycall.governance.models import MCPServer

    rows = session.exec(MCPServer.select_all()).all()
    return [_mcp_to_out(srv) for srv in rows]


@router.post("/mcp-servers", response_model=MCPServerOut, status_code=status.HTTP_201_CREATED)
def create_mcp_server(
    payload: MCPServerCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> MCPServerOut:
    """注册 MCP server。name 唯一冲突 → 409。env/headers 加密存库。

    启用的 server 自动建连 + 注册工具（调 MCPManager.reload）。"""
    from bycall.governance.models import MCPServer
    from bycall.governance.secrets import encrypt_secret

    if session.exec(MCPServer.select_by_name(payload.name)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"MCP server 名已存在: {payload.name}",
        )
    srv = MCPServer(
        name=payload.name,
        description=payload.description,
        transport=payload.transport,
        command=payload.command,
        args_json=json.dumps(payload.args) if payload.args else "[]",
        env_encrypted=encrypt_secret(json.dumps(payload.env)) if payload.env else "",
        url=payload.url,
        headers_encrypted=encrypt_secret(json.dumps(payload.headers)) if payload.headers else "",
        is_enabled=payload.is_enabled,
    )
    session.add(srv)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"MCP server 名已存在: {payload.name}"
        ) from None
    session.refresh(srv)
    _audit_admin(session, request, "create_mcp_server", f"MCPServer({srv.name})")
    # 自动建连（若启用）。
    if srv.is_enabled:
        from bycall.mcp.manager import get_mcp_manager

        get_mcp_manager().reload(session, srv.id)
    return _mcp_to_out(srv)


@router.get("/mcp-servers/{server_id}", response_model=MCPServerOut)
def get_mcp_server(
    server_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> MCPServerOut:
    """MCP server 详情，不存在 → 404。"""
    return _mcp_to_out(_get_mcp_or_404(session, server_id))


@router.put("/mcp-servers/{server_id}", response_model=MCPServerOut)
def update_mcp_server(
    server_id: int,
    payload: MCPServerUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> MCPServerOut:
    """更新 MCP server（PATCH 语义：仅传入字段生效）。

    env/headers 是 write-only：不传（None）= 不改；传 ``{}`` = 清空。
    改完自动热重载（断旧 client + 用新配置建连 + 重新注册工具）。
    """
    from bycall.governance.secrets import encrypt_secret

    srv = _get_mcp_or_404(session, server_id)
    if payload.description is not None:
        srv.description = payload.description
    if payload.transport is not None:
        srv.transport = payload.transport
    if payload.command is not None:
        srv.command = payload.command
    if payload.args is not None:
        srv.args_json = json.dumps(payload.args)
    if payload.env is not None:
        srv.env_encrypted = encrypt_secret(json.dumps(payload.env)) if payload.env else ""
    if payload.url is not None:
        srv.url = payload.url
    if payload.headers is not None:
        srv.headers_encrypted = (
            encrypt_secret(json.dumps(payload.headers)) if payload.headers else ""
        )
    if payload.is_enabled is not None:
        srv.is_enabled = payload.is_enabled
    session.add(srv)
    session.commit()
    session.refresh(srv)
    _audit_admin(session, request, "update_mcp_server", f"MCPServer({srv.name})")
    # 热重载：断旧 client + 用新配置建连（或禁用时摘除工具）。
    from bycall.mcp.manager import get_mcp_manager

    get_mcp_manager().reload(session, srv.id)
    return _mcp_to_out(srv)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(
    server_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除 MCP server。断开连接 + 摘除其工具。"""
    srv = _get_mcp_or_404(session, server_id)
    _audit_admin(session, request, "delete_mcp_server", f"MCPServer({srv.name})", before=srv)
    from bycall.mcp.manager import get_mcp_manager

    get_mcp_manager().disconnect(server_id)
    session.delete(srv)
    session.commit()


@router.post("/mcp-servers/{server_id}/reload", response_model=dict)
def reload_mcp_server(
    server_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> dict:
    """手动热重载：重新发现工具并注册（MCP server 自身加了新工具时用）。"""
    srv = _get_mcp_or_404(session, server_id)
    from bycall.mcp.manager import get_mcp_manager

    ok, msg = get_mcp_manager().reload(session, server_id)
    return {"ok": ok, "name": srv.name, "message": msg}


@router.post("/mcp-servers/{server_id}/test", response_model=MCPTestResult)
def test_mcp_server(
    server_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> MCPTestResult:
    """连通性测试：用当前配置建临时连接 → initialize + list_tools → 关闭。

    不影响 manager 的持久 client，纯验证配置是否可用。"""
    from bycall.mcp.client import MCPClient
    from bycall.mcp.manager import _server_to_config

    srv = _get_mcp_or_404(session, server_id)
    config = _server_to_config(srv)
    client = MCPClient(config, connect_timeout=20)
    try:
        client.connect()
    except Exception as e:  # 测试结果以结构化返回，不抛
        return MCPTestResult(ok=False, message=f"连接失败: {e}")
    try:
        tools = client.tools
        return MCPTestResult(
            ok=True,
            message=f"连接成功，发现 {len(tools)} 个工具",
            tool_count=len(tools),
            tools=[t.name for t in tools],
        )
    finally:
        client.close()


# ============ 审批队列（M4 Slice C HITL 完整闭环）============


def _get_operator_name(request: Request) -> str:
    """从请求 cookie 提取操作者用户名（审批人记录用）。"""
    from bycall.governance.auth import load_session_cookie

    token = request.cookies.get("bycall_session")
    if token:
        data = load_session_cookie(token)
        if data is not None:
            return data.user_name
    return "unknown"


def _resume_task_after_approval(session: Session, req) -> None:
    """审批处理后恢复关联任务（S1b 闭环：approve/reject → 任务自动推进）。

    任务处于 paused（等审批）时调 ``run_task`` resume；resume 内部按该 Step 的审批
    结果推进：全部 executed → 继续；有 rejected/failed → 该步失败 → 任务失败。
    无激活对话模型时保持 paused（配置模型后由启动续跑恢复）。
    """
    if req.task_id is None:
        return
    task = session.get(Task, req.task_id)
    if task is None or task.status != "paused":
        return

    from bycall.agent.task_engine import run_task
    from bycall.llm.resolver import get_chat_model

    resolved = get_chat_model(session)
    if resolved is None:
        logger.warning("无激活对话模型，任务 #%s 保持暂停（审批 #%s 已处理）", task.id, req.id)
        return
    _, chat_model = resolved
    try:
        run_task(session=session, task=task, chat_model=chat_model)
        logger.info("审批 #%s 后任务 #%s 已恢复", req.id, task.id)
    except Exception:
        logger.exception("审批 #%s 后任务 #%s 恢复失败", req.id, task.id)


def _get_approval_or_404(session: Session, approval_id: int):
    """按 id 取审批请求，不存在 → 404。"""
    from bycall.governance.models import ApprovalRequest

    req = session.get(ApprovalRequest, approval_id)
    if req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"审批请求不存在: {approval_id}",
        )
    return req


def _approval_to_out(req) -> ApprovalOut:
    """ApprovalRequest ORM → ApprovalOut。tool_args_json 解析为 dict。"""
    try:
        args = json.loads(req.tool_args_json) if req.tool_args_json else {}
    except (ValueError, TypeError):
        args = {}
    return ApprovalOut(
        id=req.id,
        user_id=req.user_id,
        user_name=req.user_name,
        conversation_id=req.conversation_id,
        channel=req.channel,
        tool_name=req.tool_name,
        tool_args=args,
        status=req.status,
        reviewed_by=req.reviewed_by,
        reviewed_at=req.reviewed_at,
        review_note=req.review_note,
        grant_id=req.grant_id,
        result_text=req.result_text,
        executed_at=req.executed_at,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


@router.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(
    status_filter: str | None = Query(
        default=None, alias="status", description="按状态过滤（pending/executed/rejected/failed）"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[ApprovalOut]:
    """列出审批请求（默认全部，按创建时间倒序）。可按状态过滤。"""
    from bycall.governance.models import ApprovalRequest

    if status_filter:
        rows = session.exec(ApprovalRequest.select_by_status(status_filter)).all()
    else:
        rows = session.exec(ApprovalRequest.select_all()).all()
    return [_approval_to_out(r) for r in rows[:limit]]


def _grant_to_out(grant) -> ApprovalGrantOut:
    """ApprovalGrant ORM → ApprovalGrantOut。"""
    return ApprovalGrantOut(
        id=grant.id,
        user_id=grant.user_id,
        user_name=grant.user_name,
        tool_name=grant.tool_name,
        scope=grant.scope,
        conversation_id=grant.conversation_id,
        status=grant.status,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        grant_note=grant.grant_note,
        revoked_by=grant.revoked_by,
        revoked_at=grant.revoked_at,
        created_at=grant.created_at,
        updated_at=grant.updated_at,
    )


@router.get("/approvals/grants", response_model=list[ApprovalGrantOut])
def list_approval_grants(
    status_filter: str | None = Query(
        default=None, alias="status", description="按状态过滤（active/revoked）"
    ),
    user_id: int | None = Query(default=None, description="按用户过滤"),
    tool_name: str | None = Query(default=None, description="按工具过滤"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[ApprovalGrantOut]:
    """列审批授权（T3.3 三档台账，默认全部，按授权时间倒序）。"""
    from bycall.governance.models import ApprovalGrant

    stmt = ApprovalGrant.select_all(status=status_filter)
    if user_id is not None:
        stmt = stmt.where(ApprovalGrant.user_id == user_id)
    if tool_name:
        stmt = stmt.where(ApprovalGrant.tool_name == tool_name)
    rows = session.exec(stmt).all()
    return [_grant_to_out(r) for r in rows[:limit]]


@router.post("/approvals/grants/{grant_id}/revoke", response_model=ApprovalGrantOut)
def revoke_approval_grant(
    grant_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ApprovalGrantOut:
    """撤销审批授权（软删：status=revoked + 撤销人/时间 + 审计）——立即恢复人审。

    仅 active 可撤销；已撤销的 → 409。撤销即刻生效：审批门只认 active 授权。
    """
    from bycall.governance.models import ApprovalGrant

    grant = session.get(ApprovalGrant, grant_id)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"审批授权不存在: {grant_id}",
        )
    if grant.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"审批授权已撤销（当前状态：{grant.status}），不可重复操作",
        )

    operator = _get_operator_name(request)
    grant.status = "revoked"
    grant.revoked_by = operator
    grant.revoked_at = datetime.now(UTC)
    session.add(grant)

    _audit_admin(
        session,
        request,
        "revoke_approval_grant",
        f"ApprovalGrant(#{grant.id}, {grant.tool_name}, {grant.scope}, user={grant.user_name})",
    )
    session.commit()
    session.refresh(grant)
    return _grant_to_out(grant)


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
def get_approval(
    approval_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ApprovalOut:
    """审批请求详情。"""
    return _approval_to_out(_get_approval_or_404(session, approval_id))


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut)
def approve_approval(
    approval_id: int,
    payload: ApprovalReview,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ApprovalOut:
    """审批通过：执行工具 + 记录结果 + 追加到会话历史 + 审计。

    仅 pending 状态可审批；已处理的 → 409。
    执行在独立 session 中完成（工具可能跨线程，如 MCP），结果回写本 session。
    """

    req = _get_approval_or_404(session, approval_id)
    if req.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"审批请求已处理（当前状态：{req.status}），不可重复审批",
        )

    # T3.3 三档：session 档必须绑定会话（REST 单轮无会话 → 语义不成立，明确拒绝）。
    if payload.grant_scope == "session" and req.conversation_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="会话级授权需要会话上下文（该请求来自无会话的单轮调用），请选择仅本次或永久",
        )

    # 执行工具（在独立 session + 独立 capability 上下文中）。
    from bycall.tools.approval import execute_approved_tool, push_approval_result

    ok, result_text = execute_approved_tool(req)

    from bycall.observability.metrics import HUMAN_TAKEOVER

    HUMAN_TAKEOVER.labels(kind="approved").inc()

    operator = _get_operator_name(request)
    now = datetime.now(UTC)
    req.status = "executed" if ok else "failed"
    req.result_text = result_text
    req.reviewed_by = operator
    req.reviewed_at = now
    req.executed_at = now if ok else None
    req.review_note = payload.note
    session.add(req)

    # T3.3：批准时按档位落授权（once 不落表；session/always 幂等 upsert，重复授权只刷新）。
    # 本次执行走人审路径，grant_id 不写（该列只标注"授权免审短路执行"的台账记录）。
    grant_desc = "once"
    if payload.grant_scope in ("session", "always"):
        grant = _upsert_approval_grant(session, req, payload, operator)
        grant_desc = f"{payload.grant_scope}(#{grant.id})"

    # 追加执行结果到会话历史（用户下次交互可见）。
    if req.conversation_id is not None:
        from bycall.agent.session_store import save_message

        if ok:
            msg = f"✅ 审批 #{req.id} 已通过并执行：{req.tool_name} → {result_text[:500]}"
        else:
            msg = f"❌ 审批 #{req.id} 已通过但执行失败：{result_text[:500]}"
        save_message(session, req.conversation_id, "assistant", msg)

    _audit_admin(
        session,
        request,
        "approve_tool",
        f"ApprovalRequest(#{req.id}, {req.tool_name}, grant={grant_desc})",
    )
    session.commit()
    session.refresh(req)
    # M6：只有审批状态、结果和会话历史成功提交后才通知；失败不回滚已提交事务。
    if ok:
        push_approval_result(req, result_text)
    # S1b：任务内审批 → 恢复关联任务（resume 按审批结果推进 Step）。
    _resume_task_after_approval(session, req)
    return _approval_to_out(req)


def _upsert_approval_grant(session: Session, req, payload: ApprovalReview, operator: str):
    """按 (user, tool, scope, conversation) 幂等落一条授权（T3.3）。

    已有同维度 active 授权时只刷新授予人/时间/备注（避免重复行堆叠）；否则新建。
    撤销后的授权不会复活——再次批准会新建一条，历史授权轨迹完整保留。
    """
    from sqlmodel import select

    from bycall.governance.models import ApprovalGrant

    conversation_id = req.conversation_id if payload.grant_scope == "session" else None
    existing = session.exec(
        select(ApprovalGrant).where(
            ApprovalGrant.user_id == req.user_id,
            ApprovalGrant.tool_name == req.tool_name,
            ApprovalGrant.scope == payload.grant_scope,
            ApprovalGrant.conversation_id == conversation_id,  # type: ignore[arg-type]
            ApprovalGrant.status == "active",
        )
    ).first()
    if existing is not None:
        existing.granted_by = operator
        existing.granted_at = datetime.now(UTC)
        existing.grant_note = payload.note
        session.add(existing)
        return existing
    grant = ApprovalGrant(
        user_id=req.user_id,
        user_name=req.user_name,
        tool_name=req.tool_name,
        scope=payload.grant_scope,
        conversation_id=conversation_id,
        status="active",
        granted_by=operator,
        grant_note=payload.note,
    )
    session.add(grant)
    session.flush()  # 拿到 id 供审计详情引用
    return grant


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
def reject_approval(
    approval_id: int,
    payload: ApprovalReview,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ApprovalOut:
    """拒绝审批：标记 rejected + 通知会话 + 审计。仅 pending 可拒绝。"""
    req = _get_approval_or_404(session, approval_id)
    if req.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"审批请求已处理（当前状态：{req.status}），不可重复操作",
        )

    operator = _get_operator_name(request)
    req.status = "rejected"
    req.reviewed_by = operator
    req.reviewed_at = datetime.now(UTC)
    req.review_note = payload.note
    session.add(req)

    from bycall.observability.metrics import HUMAN_TAKEOVER

    HUMAN_TAKEOVER.labels(kind="rejected").inc()

    # 通知会话：操作被拒绝。
    if req.conversation_id is not None:
        from bycall.agent.session_store import save_message

        msg = f"🚫 审批 #{req.id}（{req.tool_name}）已被拒绝。"
        if payload.note:
            msg += f" 备注：{payload.note}"
        save_message(session, req.conversation_id, "assistant", msg)

    _audit_admin(session, request, "reject_tool", f"ApprovalRequest(#{req.id}, {req.tool_name})")
    session.commit()
    session.refresh(req)
    # S1b：任务内审批被拒 → 恢复任务（resume 将对应 Step 标 failed → 任务失败）。
    _resume_task_after_approval(session, req)
    return _approval_to_out(req)


# ============ 任务执行（S1）：列表 / 详情 / SSE 进度 ============


def _task_to_out(session: Session, task: Task) -> TaskOut:
    """Task ORM → TaskOut（补 user_name，列表/详情共用）。"""
    user = session.get(User, task.user_id)
    return TaskOut(
        id=task.id,  # type: ignore[arg-type]
        user_id=task.user_id,
        user_name=user.name if user is not None else "",
        conversation_id=task.conversation_id,
        channel=task.channel,
        trigger=task.trigger,
        schedule_id=task.schedule_id,
        goal=task.goal,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _task_detail_out(session: Session, task: Task) -> TaskDetailOut:
    """任务详情：Task + Steps + Runs（进度页一次取全）。"""
    steps = list(session.exec(Step.select_by_task(task.id)))  # type: ignore[arg-type]
    runs: list[Run] = []
    for step in steps:
        runs.extend(session.exec(Run.select_by_step(step.id)))
    return TaskDetailOut(
        task=_task_to_out(session, task),
        steps=[TaskStepOut.model_validate(s) for s in steps],
        runs=[TaskRunOut.model_validate(r) for r in runs],
    )


def _get_task_or_404(session: Session, task_id: int) -> Task:
    """按 id 取任务，不存在 → 404。"""
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务不存在: {task_id}",
        )
    return task


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    status_filter: str | None = Query(
        default=None, alias="status", description="按状态过滤（completed/failed/paused/...）"
    ),
    user_id: int | None = Query(default=None, description="按执行用户过滤"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[TaskOut]:
    """列出任务（默认全部，按创建时间倒序）。admin 视角：跟进全员任务执行。"""
    stmt = select(Task)
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    rows = session.exec(stmt.order_by(Task.created_at.desc()).limit(limit)).all()
    return [_task_to_out(session, t) for t in rows]


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
def get_task(
    task_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> TaskDetailOut:
    """任务详情（Task + Steps + Runs），供进度页初始渲染。"""
    return _task_detail_out(session, _get_task_or_404(session, task_id))


@router.get("/tasks/{task_id}/events")
async def task_events(
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Response:
    """任务执行 SSE 事件流：先回放 DB 快照（snapshot 事件），再转发实时事件。

    实现要点：
    - 事件未落库（进程内总线），连接时以 DB 现状合成 ``snapshot`` 事件做初始渲染，
      之后增量追平——前端先渲染快照再按事件推进，断线重连不丢进度。
    - 订阅进程级 ``default_bus``（通配），只转发本任务事件；handler 异常已隔离。
    - 异步生成器 + ``request.is_disconnected()``：客户端断开立即退出（不依赖
      GeneratorExit 时序），15s 无事件发注释行心跳保活。
    """
    import asyncio

    from bycall.agent.events import TaskEvent, default_bus

    _get_task_or_404(session, task_id)  # 404 校验

    steps = list(session.exec(Step.select_by_task(task_id)))
    runs: list[Run] = []
    for step in steps:
        runs.extend(session.exec(Run.select_by_step(step.id)))
    snapshot = {
        "task": _task_to_out(session, session.get(Task, task_id)).model_dump(mode="json"),
        "steps": [TaskStepOut.model_validate(s).model_dump(mode="json") for s in steps],
        "runs": [TaskRunOut.model_validate(r).model_dump(mode="json") for r in runs],
    }

    async def _event_stream():
        # 先发快照，再订阅总线追平增量。
        yield f"data: {TaskEvent(type='snapshot', task_id=task_id, payload=snapshot).model_dump_json()}\n\n"

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        def _put(event: TaskEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        default_bus.subscribe(None, _put)
        try:
            while True:
                # 同时等待：事件 / 客户端断开 / 心跳超时——断开立即退出，不拖 15s。
                get_task = asyncio.create_task(queue.get())
                disc_task = asyncio.create_task(request.is_disconnected())
                done, pending = await asyncio.wait(
                    {get_task, disc_task}, timeout=15, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if disc_task in done and disc_task.result():
                    return
                if get_task in done:
                    event: TaskEvent = get_task.result()
                    if event.task_id == task_id:
                        yield f"data: {event.model_dump_json()}\n\n"
                else:
                    yield ": keepalive\n\n"  # 心跳注释行（SSE 协议空行间隔）
        finally:
            default_bus.unsubscribe(None, _put)

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


# ============ 任务调度（S1b）：定时/事件触发管理 ============


def _get_schedule_or_404(session: Session, schedule_id: int) -> TaskSchedule:
    """按 id 取调度，不存在 → 404。"""
    sched = session.get(TaskSchedule, schedule_id)
    if sched is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务调度不存在: {schedule_id}",
        )
    return sched


def _schedule_to_out(sched: TaskSchedule) -> TaskScheduleOut:
    """TaskSchedule ORM → TaskScheduleOut（task_template_json 解析为 dict）。"""
    try:
        template = json.loads(sched.task_template_json or "{}")
    except (json.JSONDecodeError, TypeError):
        template = {}
    if not isinstance(template, dict):
        template = {}
    return TaskScheduleOut(
        id=sched.id,  # type: ignore[arg-type]
        name=sched.name,
        trigger_type=sched.trigger_type,
        cron_expr=sched.cron_expr,
        event_source=sched.event_source,
        task_template=template,
        user_id=sched.user_id,
        is_enabled=sched.is_enabled,
        last_fired_at=sched.last_fired_at,
        created_at=sched.created_at,
        updated_at=sched.updated_at,
    )


def _validate_schedule_payload(payload: TaskScheduleCreate | TaskScheduleUpdate) -> None:
    """校验调度触发配置：cron/event 字段按 trigger_type 二选一，cron 表达式合法。"""
    trigger_type = payload.trigger_type
    cron_expr = payload.cron_expr
    event_source = payload.event_source
    # PATCH 语义：更新时未传的字段沿用旧值，校验只针对传入部分。
    if trigger_type is None:
        return
    if trigger_type not in ("cron", "event"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"trigger_type 必须是 cron 或 event（收到: {trigger_type}）",
        )
    if trigger_type == "cron":
        if not cron_expr:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="trigger_type=cron 时必须提供 cron_expr",
            )
        from croniter import croniter

        if not croniter.is_valid(cron_expr):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"cron 表达式非法: {cron_expr}",
            )
    else:
        if not event_source:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="trigger_type=event 时必须提供 event_source",
            )


@router.get("/task-schedules", response_model=list[TaskScheduleOut])
def list_task_schedules(
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[TaskScheduleOut]:
    """列出全部任务调度（按 id 升序）。"""
    rows = session.exec(select(TaskSchedule).order_by(TaskSchedule.id)).all()
    return [_schedule_to_out(r) for r in rows]


@router.post("/task-schedules", response_model=TaskScheduleOut, status_code=201)
def create_task_schedule(
    payload: TaskScheduleCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> TaskScheduleOut:
    """创建任务调度（cron 定时 / event 事件触发）。"""
    _validate_schedule_payload(payload)
    if session.get(User, payload.user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"执行用户不存在: {payload.user_id}",
        )
    sched = TaskSchedule(
        name=payload.name,
        trigger_type=payload.trigger_type,
        cron_expr=payload.cron_expr,
        event_source=payload.event_source,
        task_template_json=json.dumps(payload.task_template, ensure_ascii=False),
        user_id=payload.user_id,
        is_enabled=payload.is_enabled,
    )
    try:
        session.add(sched)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"调度名已存在: {payload.name}",
        ) from None
    session.refresh(sched)
    _audit_admin(
        session, request, "create_task_schedule", f"TaskSchedule(#{sched.id}, {sched.name})"
    )
    return _schedule_to_out(sched)


@router.get("/task-schedules/{schedule_id}", response_model=TaskScheduleOut)
def get_task_schedule(
    schedule_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> TaskScheduleOut:
    """任务调度详情。"""
    return _schedule_to_out(_get_schedule_or_404(session, schedule_id))


@router.patch("/task-schedules/{schedule_id}", response_model=TaskScheduleOut)
def update_task_schedule(
    schedule_id: int,
    payload: TaskScheduleUpdate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> TaskScheduleOut:
    """更新任务调度（PATCH 语义：仅传入字段生效）。"""
    sched = _get_schedule_or_404(session, schedule_id)
    # 合并旧值后整体校验（PATCH 只校验最终形态）。
    merged = TaskScheduleCreate(
        name=payload.name or sched.name,
        trigger_type=payload.trigger_type or sched.trigger_type,
        cron_expr=payload.cron_expr if payload.cron_expr is not None else sched.cron_expr,
        event_source=(
            payload.event_source if payload.event_source is not None else sched.event_source
        ),
        task_template=(
            payload.task_template
            if payload.task_template is not None
            else (json.loads(sched.task_template_json or "{}") or {})
        ),
        user_id=payload.user_id if payload.user_id is not None else sched.user_id,
        is_enabled=payload.is_enabled if payload.is_enabled is not None else sched.is_enabled,
    )
    _validate_schedule_payload(merged)
    if session.get(User, merged.user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"执行用户不存在: {merged.user_id}",
        )

    sched.name = merged.name
    sched.trigger_type = merged.trigger_type
    sched.cron_expr = merged.cron_expr
    sched.event_source = merged.event_source
    sched.task_template_json = json.dumps(merged.task_template, ensure_ascii=False)
    sched.user_id = merged.user_id
    sched.is_enabled = merged.is_enabled
    try:
        session.add(sched)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"调度名已存在: {merged.name}",
        ) from None
    session.refresh(sched)
    _audit_admin(
        session, request, "update_task_schedule", f"TaskSchedule(#{sched.id}, {sched.name})"
    )
    return _schedule_to_out(sched)


@router.delete("/task-schedules/{schedule_id}", status_code=204)
def delete_task_schedule(
    schedule_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> None:
    """删除任务调度（不影响已触发的任务）。"""
    sched = _get_schedule_or_404(session, schedule_id)
    name = sched.name
    session.delete(sched)
    session.commit()
    _audit_admin(session, request, "delete_task_schedule", f"TaskSchedule(#{schedule_id}, {name})")


@router.post("/task-schedules/{schedule_id}/fire", response_model=TaskOut)
def fire_task_schedule(
    schedule_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> TaskOut:
    """手动触发一次调度（测试/立即执行用），与定时/事件触发走同一引擎。"""
    from bycall.agent.task_engine import fire_schedule_task
    from bycall.llm.resolver import get_chat_model

    sched = _get_schedule_or_404(session, schedule_id)
    resolved = get_chat_model(session)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="未激活对话模型，无法执行任务",
        )
    _, chat_model = resolved
    task = fire_schedule_task(session=session, schedule=sched, chat_model=chat_model)
    sched.last_fired_at = datetime.now(UTC)
    session.add(sched)
    session.commit()
    _audit_admin(
        session,
        request,
        "fire_task_schedule",
        f"TaskSchedule(#{schedule_id}, {sched.name}) → Task(#{task.id})",
    )
    return _task_to_out(session, task)
