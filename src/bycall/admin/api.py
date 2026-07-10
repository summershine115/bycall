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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from bycall.admin.schemas import (
    AuditOut,
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
    Role,
    SystemPrompt,
    SystemSetting,
    User,
)
from bycall.governance.security import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 内置保留角色名：不允许在 Role 表里建记录（policy 引擎对 "admin" 直接放全权）。
_RESERVED_ROLE_NAMES = frozenset({"admin"})


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
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
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


# ============ 角色 ============


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
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
    session: Session = Depends(get_session),
    _admin: None = Depends(_require_admin),
) -> Role:
    """更新角色的 description / config。不存在 → 404。"""
    role = _require_role_exists(session, name)
    if payload.description is not None:
        role.description = payload.description
    if payload.config is not None:
        role.config = payload.config
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@router.delete("/roles/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    name: str,
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
