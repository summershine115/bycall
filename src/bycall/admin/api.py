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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from bycall.admin.schemas import (
    AuditOut,
    ConversationOut,
    DocumentCreate,
    DocumentIngestResult,
    DocumentOut,
    DocumentUpdate,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
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
    RoleCreate,
    RoleOut,
    RoleUpdate,
    SettingOut,
    SettingsBatchUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)
from bycall.db import get_session
from bycall.governance.models import (
    AuditLog,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    Model,
    Role,
    SystemPrompt,
    SystemSetting,
    User,
    UserMemory,
)
from bycall.governance.security import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 内置保留角色名：不允许在 Role 表里建记录（policy 引擎对 "admin" 直接放全权）。
_RESERVED_ROLE_NAMES = frozenset({"admin"})

# 上传文件大小上限：10MB。超过 → 413，避免超大文件全量读入内存导致 OOM。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# 审计日志中需要脱敏的字段名（值不落审计，只记 "<masked>"）。
_SENSITIVE_FIELDS = frozenset({"password_hash", "api_key", "value", "secret"})


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
    import json

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


# ============ 用户 ============


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    request: Request,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> User:
    """创建用户。密码哈希后入库；name 唯一，冲突 → 409。"""
    user = User(
        name=payload.name,
        display_name=payload.display_name,
        role=payload.role,
        password_hash=hash_password(payload.password) if payload.password else "",
        is_active=payload.is_active,
    )
    session.add(user)
    try:
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
    import json

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

    session.add(user)
    session.commit()
    session.refresh(user)
    _audit_admin(
        session, request, "update_user", f"User({user.name})",
        before=json.loads(before_snapshot), after=user,
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
    import json

    before_snapshot = json.dumps(role.model_dump(), default=str)
    if payload.description is not None:
        role.description = payload.description
    if payload.config is not None:
        role.config = payload.config
    session.add(role)
    session.commit()
    session.refresh(role)
    _audit_admin(
        session, request, "update_role", f"Role({role.name})",
        before=json.loads(before_snapshot), after=role,
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
    from bycall.governance.settings_store import mask_secret

    rows = session.exec(SystemSetting.select_all()).all()
    out = []
    for r in rows:
        value = mask_secret(r.value) if r.is_secret else r.value
        out.append(
            {
                "key": r.key,
                "value": value,
                "is_secret": r.is_secret,
                "description": r.description,
                "is_set": bool(r.value),
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

    - secret 字段：value 为脱敏占位符（含 ***）时视为"未改"，跳过。
    - value 空串：清除该 key（回退到 .env/默认）。
    """
    from bycall.governance.settings_store import set_setting_value

    skipped: list[str] = []
    updated: list[str] = []
    for key, value in body.settings.items():
        existing = session.exec(SystemSetting.select_by_key(key)).first()
        if existing is None:
            skipped.append(f"{key}（未知 key）")
            continue
        # secret 占位符保护：脱敏值里含 *** 且不是空串 → 用户没真改，跳过。
        if existing.is_secret and "***" in value and value != "":
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
        session, request, "update_settings", ",".join(updated),
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
    from bycall.governance.settings_store import mask_secret

    return ModelOut(
        id=model.id,  # type: ignore[arg-type]
        name=model.name,
        display_name=model.display_name,
        model_type=model.model_type,
        source=model.source,
        provider=model.provider,
        base_url=model.base_url,
        api_key_masked=mask_secret(model.api_key),
        has_api_key=bool(model.api_key),
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

    model = Model(
        name=payload.name,
        display_name=payload.display_name,
        model_type=payload.model_type,
        source=payload.source,
        provider=payload.provider,
        base_url=payload.base_url,
        api_key=payload.api_key,
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
        model.api_key = payload.api_key

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
    """删除模型。``is_active=True`` 的不能删（避免误删当前生效模型）→ 400。"""
    model = _get_model_or_404(session, model_id)
    if model.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除当前激活的模型，请先激活其他同类模型再删",
        )
    _audit_admin(
        session, request, "delete_model", f"Model({model.name})", before=model
    )
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
    _audit_admin(
        session, request, "activate_model", f"Model({model.name})", after=model
    )
    session.commit()
    return {"ok": True, "model": _model_to_out(model).model_dump(mode="json")}


@router.post("/models/{model_id}/test", response_model=ModelTestResult)
def test_model(
    model_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> ModelTestResult:
    """连通性测试：按 provider 构造临时 OpenAI 兼容 client 发一次最小请求，计时。

    - chat：``chat.completions.create`` 一条 "hi"；
    - embedding：``embeddings.create`` 一个 "test"，并校验返回维度 == 配置维度；
    - 异常一律捕获返回 ``ok=False`` + error，绝不抛 500（前端要拿结果展示）。
    """
    import time

    model = _get_model_or_404(session, model_id)

    try:
        from openai import OpenAI
    except ImportError as exc:  # openai 未装：返失败而非 500
        return ModelTestResult(ok=False, error=f"未安装 openai 客户端: {exc}")

    start = time.perf_counter()
    try:
        client = OpenAI(
            base_url=model.base_url or None,
            api_key=model.api_key or "EMPTY",
            timeout=15,
        )
        if model.model_type == "embedding":
            resp = client.embeddings.create(model=model.name, input="test")
            dim = len(resp.data[0].embedding) if resp.data else 0
            latency_ms = int((time.perf_counter() - start) * 1000)
            if model.embed_dimension and dim != model.embed_dimension:
                return ModelTestResult(
                    ok=False,
                    latency_ms=latency_ms,
                    error=f"维度不匹配：期望 {model.embed_dimension}，实际 {dim}",
                )
            detail = f"dim={dim}, model={resp.model}"
            return ModelTestResult(ok=True, latency_ms=latency_ms, detail=detail)
        else:
            # chat / rerank（rerank 暂按 chat 路径尝试，OpenAI 兼容性有限）
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
        store.delete_by_filter(
            [FilterCondition(field="doc_id", op="match", value=f"doc_{doc_id}")]
        )
        store.flush()
    finally:
        store.close()


def _ingest_document(
    session: Session,
    kb: KnowledgeBase,
    doc_id: int,
    title: str,
    source: str,
    content: str,
) -> DocumentIngestResult:
    """同步入库：标记 processing → 用 KB 的 embedding 模型 → 删旧 chunk → 入库 → 更新。

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

    # 2. 用 KB 绑定的 embedding 模型。
    embed_entity = session.get(Model, kb.embedding_model_id)
    if embed_entity is None:
        doc.status = "failed"
        doc.error_message = f"KB 绑定的 embedding 模型不存在 (id={kb.embedding_model_id})"
        session.add(doc)
        session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=doc.error_message)

    embed_model = resolve_embed_model(session, embed_entity)

    # 3. 删旧 chunk（幂等，支持 reindex）。
    try:
        _delete_chunks_for_doc(kb, doc_id)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("删旧 chunk 失败 (doc_id=%s): %s", doc_id, exc)

    # 4. 入库（用 KB 的 collection + 维度）。
    errors: list[str] = []
    chunk_count = 0
    try:
        store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
        try:
            pipeline = IngestPipeline(store, embed_model, collection_name=kb.collection_name)
            result = pipeline.ingest_text(content, source=source, doc_id=f"doc_{doc_id}")
            errors = result.errors
            chunk_count = result.chunk_count
        finally:
            store.close()
    except Exception as exc:
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
    session.add(kb)
    session.commit()
    session.refresh(doc)

    return DocumentIngestResult(
        ok=doc.status == "completed",
        document_id=doc_id,
        chunk_count=chunk_count,
        errors=errors,
    )


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
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> list[KnowledgeBase]:
    """列出全部知识库。"""
    return list(session.exec(KnowledgeBase.select_all()).all())


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
    """更新知识库名称/描述。不改 embedding 模型（维度锁定）。"""
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

    # 删 zvec collection
    try:
        store = create_vector_store(get_settings().vector_store, dimension=kb.dimension)
        store.destroy(kb.collection_name)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("删除 collection 失败 (kb=%s): %s", kb_id, exc)

    # 删全部文档
    docs = session.exec(Document.select_by_kb(kb_id)).all()
    for doc in docs:
        session.delete(doc)

    # 审计（在删 KB 记录前记录）
    _audit_admin(
        session, request, "delete_knowledge_base", f"KB({kb.name})", before=kb
    )

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
            select(Document).where(
                Document.knowledge_base_id == kb_id, Document.source == source
            )
        ).all()
    else:
        rows = session.exec(Document.select_by_kb(kb_id)).all()
    return list(rows)


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentIngestResult,
    status_code=status.HTTP_201_CREATED,
)
def create_document(
    kb_id: int,
    payload: DocumentCreate,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> DocumentIngestResult:
    """在指定 KB 内创建文档并入库。"""
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
        status="pending",
    )
    session.add(doc)
    kb.document_count += 1
    session.add(kb)
    session.commit()
    session.refresh(doc)

    return _ingest_document(session, kb, doc.id, doc.title, doc.source, doc.content)


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
    if payload.title is not None:
        doc.title = payload.title
    if payload.source is not None and payload.source != doc.source:
        doc.source = payload.source.strip()
        need_reingest = True

    session.add(doc)
    session.commit()
    session.refresh(doc)

    if need_reingest:
        _ingest_document(session, kb, doc.id, doc.title, doc.source, doc.content)

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
        import logging

        logging.getLogger(__name__).warning("删除文档 chunk 失败 (doc_id=%s): %s", doc_id, exc)

    # 审计（在删记录前记录）
    _audit_admin(
        session, request, "delete_document", f"Doc({doc.title}, kb={kb.name})", before=doc
    )

    # 更新 KB 统计
    kb.chunk_count -= doc.chunk_count
    kb.document_count -= 1
    session.add(kb)

    session.delete(doc)
    session.commit()


@router.post(
    "/knowledge-bases/{kb_id}/documents/{doc_id}/reindex",
    response_model=DocumentIngestResult,
)
def reindex_document(
    kb_id: int,
    doc_id: int,
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> DocumentIngestResult:
    """重新入库。"""
    kb = _get_kb_or_404(session, kb_id)
    doc = _get_document_or_404(session, doc_id)
    if doc.knowledge_base_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文档不属于该知识库: {doc_id}")
    return _ingest_document(session, kb, doc.id, doc.title, doc.source, doc.content)


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
    response_model=DocumentIngestResult,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: int,
    file: UploadFile,
    source: str = Query(..., description="知识域标签"),
    title: str | None = Query(default=None, description="文档标题（默认用文件名）"),
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> DocumentIngestResult:
    """文件上传入库（到指定 KB）。"""
    kb = _get_kb_or_404(session, kb_id)

    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件名不能为空")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("txt", "md", "markdown"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: .{ext}（仅支持 txt/md/markdown）",
        )

    # 文件大小限制：最多读 MAX_UPLOAD_BYTES + 1 字节，超限 → 413。
    # 避免全量 file.read() 把超大文件读进内存导致 OOM。
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB）",
        )

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="文件不是有效的 UTF-8 文本",
        ) from exc

    content = content.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件内容为空")

    doc_title = title or file.filename.rsplit(".", 1)[0]

    doc = Document(
        knowledge_base_id=kb_id,
        title=doc_title,
        source=source.strip(),
        content=content,
        file_name=file.filename,
        file_type=ext,
        status="pending",
    )
    session.add(doc)
    kb.document_count += 1
    session.add(kb)
    session.commit()
    session.refresh(doc)

    return _ingest_document(session, kb, doc.id, doc.title, doc.source, doc.content)


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
