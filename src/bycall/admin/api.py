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
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from bycall.admin.schemas import (
    ApprovalOut,
    ApprovalReview,
    AuditOut,
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
    PromptActivate,
    PromptCreate,
    PromptOut,
    PromptUpdate,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    SettingOut,
    SettingsBatchUpdate,
    SkillCreate,
    SkillOut,
    SkillUpdate,
    ToolOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from bycall.db import get_session, session_scope
from bycall.governance.models import (
    AuditLog,
    Conversation,
    Document,
    IMBinding,
    KnowledgeBase,
    Message,
    Model,
    Role,
    Skill,
    SystemPrompt,
    SystemSetting,
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
    LLM 完成某领域任务。创建后默认 ``is_enabled=True``，admin 可在角色配置里绑定。
    """
    skill = Skill(
        name=payload.name,
        description=payload.description,
        content=payload.content,
        is_enabled=True,
    )
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


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    limit: int = Query(default=100, ge=1, le=500, description="返回最近 N 条审计记录"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[AuditLog]:
    """返回最近 N 条审计记录（按时间倒序）。limit ∈ [1, 500]。"""
    logs = session.exec(AuditLog.select_recent(limit=limit)).all()
    return list(logs)


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


# ============ IM 渠道热重载 ============


@router.post("/im/reload", response_model=dict)
def reload_im_channel(
    _admin: None = Depends(_require_admin),
) -> dict:
    """热重载 IM 渠道：断开旧连接 + 用当前凭证建新连接。不重启 HTTP 服务。

    admin 改完 IM 凭证后调用此端点（或前端保存设置时自动触发）。
    借鉴 OpenClaw 的 channel reload：只重启那一个渠道，其他不动。
    """
    from bycall.gateway.manager import get_channel_manager

    mgr = get_channel_manager()
    ok, msg = mgr.reload()
    return {"ok": ok, "message": msg, "status": mgr.status()}


@router.get("/im/status", response_model=dict)
def im_status(
    _admin: None = Depends(_require_admin),
) -> dict:
    """查询 IM 渠道当前连接状态。"""
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
    """更新文档标题/source。改 source 会重新入库。"""
    kb = _get_kb_or_404(session, kb_id)
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")

    need_reingest = False
    old_source = doc.source
    if payload.title is not None:
        doc.title = payload.title
    if payload.source is not None and payload.source != doc.source:
        new_source = payload.source.strip()
        if not new_source:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="知识域标签不能为空")
        doc.source = new_source
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
            # 旧 chunk 若仍存在，其 ACL 仍是 old_source；DB 必须回滚到相同授权状态。
            doc.source = old_source
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
    title: str | None = Query(default=None, description="文档标题（默认用文件名）"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> IngestAccepted:
    """文件上传入库（到指定 KB）——同步解析 + 异步入库。

    支持格式：PDF / Word (.docx/.doc) / Markdown / HTML / TXT / CSV / JSON / 代码等。
    文件读取与解析**同步**执行（解析错误立即返回 4xx，文件内容就在手上），
    解析出正文后建文档、置 processing、提交后台入库，立即返回 IngestAccepted。
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

    # 用文档解析器提取文本。
    from bycall.rag.parsers import get_parser, list_parsers

    supported_exts = set()
    for p in list_parsers():
        supported_exts.update(p["file_types"])
    if ext not in supported_exts:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: .{ext}",
        )

    parser = get_parser(ext)
    if parser is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"未找到 .{ext} 的解析器",
        )

    try:
        result = parser.parse(raw, file_name=file.filename)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    content = result.text.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件内容为空")

    doc_title = title or result.title or file.filename.rsplit(".", 1)[0]

    doc = Document(
        knowledge_base_id=kb_id,
        title=doc_title,
        source=source.strip(),
        content=content,
        file_name=file.filename,
        file_type=ext,
        status="processing",
    )
    session.add(doc)
    kb.document_count += 1
    session.add(kb)
    session.commit()
    session.refresh(doc)

    # 仅入库异步：解析已同步完成（content 在手），后台跑切块→向量化→写库。
    _submit_ingest(kb_id, doc.id, doc.title, doc.source, content)
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

    # 执行工具（在独立 session + 独立 capability 上下文中）。
    from bycall.tools.approval import execute_approved_tool

    ok, result_text = execute_approved_tool(req)

    operator = _get_operator_name(request)
    now = datetime.now(UTC)
    req.status = "executed" if ok else "failed"
    req.result_text = result_text
    req.reviewed_by = operator
    req.reviewed_at = now
    req.executed_at = now if ok else None
    req.review_note = payload.note
    session.add(req)

    # 追加执行结果到会话历史（用户下次交互可见）。
    if req.conversation_id is not None:
        from bycall.agent.session_store import save_message

        if ok:
            msg = f"✅ 审批 #{req.id} 已通过并执行：{req.tool_name} → {result_text[:500]}"
        else:
            msg = f"❌ 审批 #{req.id} 已通过但执行失败：{result_text[:500]}"
        save_message(session, req.conversation_id, "assistant", msg)

    _audit_admin(session, request, "approve_tool", f"ApprovalRequest(#{req.id}, {req.tool_name})")
    session.commit()
    session.refresh(req)
    return _approval_to_out(req)


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
    return _approval_to_out(req)
