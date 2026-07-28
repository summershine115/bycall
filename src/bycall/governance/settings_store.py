"""运行时设置存储层：DB 值优先，.env 环境变量兜底，代码默认最后。

这是 Bycall 的配置真值层——admin 在控制面改 LLM/IM 凭证后，下一条消息即时生效。
与 Hermes/OpenClaw 的「控制台管理一切」对齐，但真值在 DB（而非写回 .env 文件），
更贴合 Bycall 已有的治理模型（User/Role/Prompt 都在 DB）。

优先级（高 → 低）：DB SystemSetting > .env 环境变量 > 代码默认值。

使用：``get_setting_value("llm.api_key", default="...", env_var="BYCALL_LLM_API_KEY")``
"""

from __future__ import annotations

import logging
import os

from sqlmodel import Session

from bycall.governance.models import SystemSetting

logger = logging.getLogger(__name__)


def get_setting_value(
    key: str,
    *,
    default: str = "",
    env_var: str | None = None,
    session: Session | None = None,
) -> str:
    """读一个设置值。优先级：DB > 环境变量 > default。

    若提供 session 则用之；否则新建（适用于非请求上下文，如 IM handler）。
    DB 值为空串时视为"未设"，回退到环境变量/默认（避免空串覆盖有意义的环境值）。
    """
    db_value = _read_db(key, session)
    if db_value is not None and db_value != "":
        return db_value
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return default


def get_setting_int(
    key: str,
    *,
    default: int,
    env_var: str | None = None,
    session: Session | None = None,
) -> int:
    """整型设置。"""
    raw = get_setting_value(key, default=str(default), env_var=env_var, session=session)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def get_setting_float(
    key: str,
    *,
    default: float,
    env_var: str | None = None,
    session: Session | None = None,
) -> float:
    """浮点设置（解析引擎的 timeout / poll_interval 等）。非法值回退默认。"""
    raw = get_setting_value(key, default=str(default), env_var=env_var, session=session)
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def get_setting_bool(
    key: str,
    *,
    default: bool,
    env_var: str | None = None,
    session: Session | None = None,
) -> bool:
    """布尔设置。接受 true/false/1/0/yes/no（大小写不敏感）。"""
    raw = get_setting_value(key, default=str(default).lower(), env_var=env_var, session=session)
    return raw.strip().lower() in ("true", "1", "yes", "on")


def set_setting_value(
    session: Session,
    key: str,
    value: str,
    *,
    is_secret: bool = False,
    description: str = "",
) -> None:
    """写一个设置值（upsert）。调用方负责 commit。"""
    if is_secret:
        from bycall.governance.secrets import encrypt_secret

        value = encrypt_secret(value)
    existing = session.exec(SystemSetting.select_by_key(key)).first()
    if existing is None:
        session.add(
            SystemSetting(key=key, value=value, is_secret=is_secret, description=description)
        )
    else:
        existing.value = value
        if description:
            existing.description = description
        existing.is_secret = is_secret
        session.add(existing)


def _read_db(key: str, session: Session | None) -> str | None:
    """从 DB 读一个 key。无 session 时新建；key 不存在返回 None。

    DB 不可用（如未配置/连接失败）时返回 None（降级到 .env/默认），不抛异常——
    这样系统在 DB 尚未就绪时仍能用 .env 环境变量启动。
    """
    owns_session = False
    if session is None:
        try:
            from bycall.db import session_scope

            ctx = session_scope()
            session = ctx.__enter__()
            owns_session = True
        except Exception:
            logger.debug("settings_store: DB 不可用，降级到环境变量")
            return None
    try:
        row = session.exec(SystemSetting.select_by_key(key)).first()
        if row is None:
            return None
        if row.is_secret:
            from bycall.governance.secrets import decrypt_secret

            return decrypt_secret(row.value)
        return row.value
    except Exception:
        logger.debug("settings_store: 读取 %s 失败，降级到环境变量", key)
        return None
    finally:
        if owns_session:
            import contextlib

            with contextlib.suppress(Exception):
                ctx.__exit__(None, None, None)


def mask_secret(value: str) -> str:
    """脱敏：只显示前 4 + 后 4，中间用 * 代替。空值返回 '(未设置)'。"""
    if not value:
        return "(未设置)"
    if len(value) <= 12:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


__all__ = [
    "get_setting_bool",
    "get_setting_float",
    "get_setting_int",
    "get_setting_value",
    "mask_secret",
    "set_setting_value",
]
