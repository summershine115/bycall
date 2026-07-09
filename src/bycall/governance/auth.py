"""Admin 会话认证 + dev act-as-user。

- admin 登录走账号密码（密码哈希存 User.password_hash）。
- 会话用 itsdangerous 签名的 cookie（不引入额外 session 依赖）。
- dev 模式下 admin 可"扮演"普通用户（act_as），便于在无真实 IM 时测试数据面权限链路。
  生产环境（env=prod）禁止 act-as。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session

from bycall.governance.models import User
from bycall.governance.security import verify_password


@dataclass
class SessionData:
    """会话内容。签进 cookie。"""

    user_id: int
    user_name: str
    role: str
    is_admin: bool
    act_as_user_id: int | None = None  # dev: admin 扮演的普通用户 id（None=不扮演）


def create_session_serializer() -> URLSafeTimedSerializer:
    """从 config 取 session_secret 构造签名器。"""
    from bycall.config import get_settings

    return URLSafeTimedSerializer(get_settings().session_secret, salt="bycall-session")


def make_session_cookie(data: SessionData) -> str:
    """SessionData → 签名字符串（存 cookie）。"""
    return create_session_serializer().dumps(asdict(data))


def load_session_cookie(token: str, max_age: int = 86400) -> SessionData | None:
    """验签并返回 SessionData。过期/篡改返回 None。"""
    try:
        payload = create_session_serializer().loads(token, max_age=max_age)
    except BadSignature:
        return None
    try:
        return SessionData(
            user_id=int(payload["user_id"]),
            user_name=str(payload["user_name"]),
            role=str(payload["role"]),
            is_admin=bool(payload["is_admin"]),
            act_as_user_id=int(payload["act_as_user_id"])
            if payload.get("act_as_user_id") is not None
            else None,
        )
    except (KeyError, TypeError, ValueError):
        # payload 结构异常（非预期字段/类型），视作无效会话。
        return None


def login(session: Session, name: str, password: str) -> SessionData | None:
    """验证账号密码，成功返回 SessionData（失败返回 None）。

    用 verify_password 校验。只允许 is_active 的用户登录。
    """
    user = session.exec(User.select_by_name(name)).first()
    if user is None:
        return None
    if not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return _session_data_for_user(user)


def set_act_as(session: Session, admin: User, target_user_id: int) -> SessionData:
    """admin 扮演某普通用户。返回新的 SessionData（act_as_user_id=target）。

    仅 dev 环境（config.env=='dev'）允许；prod 抛 PermissionError。
    仅 admin（role=='admin'）可操作。target 必须 is_active 且不是 admin。
    """
    from bycall.config import get_settings

    # 环境守卫：prod 禁止 act-as。
    if get_settings().env != "dev":
        raise PermissionError("act-as-user 仅在 dev 环境允许")

    # 操作者守卫：必须是 admin。
    if admin.role != "admin":
        raise PermissionError("仅 admin 可执行 act-as-user")

    # 目标守卫：必须存在、is_active、且不是 admin（不能扮演另一个 admin）。
    target = session.get(User, target_user_id)
    if target is None or not target.is_active or target.role == "admin":
        raise PermissionError("act-as 目标无效（不存在/已禁用/是 admin）")

    return SessionData(
        user_id=admin.id,
        user_name=admin.name,
        role=admin.role,
        is_admin=True,
        act_as_user_id=target.id,
    )


def clear_act_as(session: Session, admin: User) -> SessionData:
    """清除扮演，回到 admin 身份。"""
    from bycall.config import get_settings

    # 与 set_act_as 保持一致的环境守卫（prod 下 act-as 本就不可能发生，但对称地拒绝）。
    if get_settings().env != "dev":
        raise PermissionError("act-as-user 仅在 dev 环境允许")
    if admin.role != "admin":
        raise PermissionError("仅 admin 可执行 act-as-user")

    return _session_data_for_user(admin)


def _session_data_for_user(user: User) -> SessionData:
    """User → SessionData（无扮演）。is_admin 按角色判定。"""
    return SessionData(
        user_id=user.id,
        user_name=user.name,
        role=user.role,
        is_admin=user.role == "admin",
        act_as_user_id=None,
    )
