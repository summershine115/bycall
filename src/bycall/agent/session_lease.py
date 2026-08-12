"""会话租约——串行化同一会话的并发访问（参考 QM ``postgres-session-store``）。

同一会话（conversation）并发消息会竞争历史读写：两条消息同时 append 导致乱序、压缩
与对话并发写 summary 损坏状态。租约保证「同一会话同一时刻只有一个 holder」：

- **原子获取**：单条 ``INSERT ... ON CONFLICT DO UPDATE ... WHERE expires_at <= now``，只有
  当前无主或已过期时才能拿到（SQLite 3.24+ 与 PostgreSQL 均支持，避免读-改-写 TOCTOU）。
- **token 所有权**：获取时生成随机 token；释放/续租都校验 token，只有持有者能操作。
- **TTL + 心跳续租**：持有期间定期续租（默认 TTL/3）；进程死亡后心跳停止，TTL 到期自动
  释放，租约可被重新获取（无需依赖 finally 释放——进程崩了 finally 不会执行）。
- **holder 类型**：``turn``（一轮对话）与 ``compaction``（后台压缩）。两者都要持租约才能
  append 会话条目（见 ``im_handler._run_stream``：turn 租约的存活区即历史 append 区）。

租约操作各用独立 session（与流式 session 解耦），避免与流式 session 的事务状态相互
干扰——心跳与流式执行并发，不能共用一个非线程/协程安全的 SQLAlchemy session。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session, select

from bycall.config import get_settings
from bycall.db import session_scope
from bycall.governance.models import SessionLease

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    """统一的当前时间（timezone-aware UTC）。"""
    return datetime.now(UTC)


# 原子获取租约：INSERT 新行，冲突时仅当旧租约已过期才抢占更新。
# 单条语句保证原子性，杜绝「读到可抢 → 被别人抢走 → 自己再写」的 TOCTOU。
_ACQUIRE_SQL = text(
    """
    INSERT INTO sessionlease (conversation_id, holder, token, acquired_at, expires_at)
    VALUES (:conversation_id, :holder, :token, :now, :expires_at)
    ON CONFLICT(conversation_id) DO UPDATE SET
        holder = :holder,
        token = :token,
        acquired_at = :now,
        expires_at = :expires_at
    WHERE sessionlease.expires_at <= :now
    """
)


def acquire_lease(
    session: Session, conversation_id: int, holder: str, *, ttl_seconds: int
) -> str | None:
    """原子获取租约。成功返回所有权 token，被他人持有（且未过期）返回 None。

    生成随机 token 并执行条件 UPSERT；随后回查确认当前持有者是否是自己（token 匹配）。
    匹配即获取成功，否则说明已有未过期的他人租约。
    """
    token = secrets.token_hex(8)
    now = _now_utc()
    expires_at = now + timedelta(seconds=ttl_seconds)
    session.execute(
        _ACQUIRE_SQL,
        {
            "conversation_id": conversation_id,
            "holder": holder,
            "token": token,
            "now": now,
            "expires_at": expires_at,
        },
    )
    row = session.exec(
        select(SessionLease).where(SessionLease.conversation_id == conversation_id)
    ).first()
    if row is not None and row.token == token:
        return token
    return None


def renew_lease(session: Session, conversation_id: int, token: str, *, ttl_seconds: int) -> bool:
    """续租：仅当 token 仍持有该会话时延长 expires_at。返回是否续租成功。

    失败（返回 False）意味着租约已被抢占或丢失——调用方应视为自己不再持有。
    """
    expires_at = _now_utc() + timedelta(seconds=ttl_seconds)
    result = session.execute(
        text(
            "UPDATE sessionlease SET expires_at = :expires_at "
            "WHERE conversation_id = :conversation_id AND token = :token"
        ),
        {"expires_at": expires_at, "conversation_id": conversation_id, "token": token},
    )
    return (result.rowcount or 0) > 0


def release_lease(session: Session, conversation_id: int, token: str) -> bool:
    """释放租约：仅当 token 匹配时删除。返回是否成功释放（失败说明已被抢占，无需再释放）。"""
    result = session.execute(
        text(
            "DELETE FROM sessionlease WHERE conversation_id = :conversation_id AND token = :token"
        ),
        {"conversation_id": conversation_id, "token": token},
    )
    return (result.rowcount or 0) > 0


def current_holder(session: Session, conversation_id: int) -> SessionLease | None:
    """查当前租约（谁持有、何时过期）。拿不到租约时用于排队提示。"""
    return session.exec(
        select(SessionLease).where(SessionLease.conversation_id == conversation_id)
    ).first()


# ============ turn 专用便捷封装（独立 session，提交后立即可见）============


def acquire_turn_lease(conversation_id: int, *, ttl_seconds: int | None = None) -> str | None:
    """获取 turn 租约（独立 session，立即提交）。成功返回 token，被占用返回 None。"""
    if ttl_seconds is None:
        ttl_seconds = get_settings().session_lease_ttl_seconds
    token = secrets.token_hex(8)
    now = _now_utc()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with session_scope() as session:
        session.execute(
            _ACQUIRE_SQL,
            {
                "conversation_id": conversation_id,
                "holder": "turn",
                "token": token,
                "now": now,
                "expires_at": expires_at,
            },
        )
        row = session.exec(
            select(SessionLease).where(SessionLease.conversation_id == conversation_id)
        ).first()
        # 在 session 关闭前读取（session_scope 退出即 commit+关闭，会过期实例属性）。
        owned = row is not None and row.token == token
    return token if owned else None


def release_turn_lease(conversation_id: int, token: str) -> None:
    """释放 turn 租约（独立 session）。异常吞掉——释放失败不应影响主流程（租约会自然过期）。"""
    try:
        with session_scope() as session:
            release_lease(session, conversation_id, token)
    except Exception:
        logger.warning("释放 turn 租约失败 conv=%s（将自然过期）", conversation_id, exc_info=True)


def lease_heartbeat(
    conversation_id: int,
    token: str,
    *,
    ttl_seconds: int,
    interval_seconds: float,
    max_failures: int,
) -> Awaitable[None]:
    """后台续租协程：定期延长 expires_at。

    - 续租成功 → 复位失败计数。
    - 续租返回 False（租约被抢占/丢失）→ 停止续租（自己已不持有，续也无意义）。
    - 续租异常（如 DB 不可达）→ 累计失败；连续 ``max_failures`` 次后停止续租，租约将
      因 TTL 自然过期——视为进程「心跳丢失」，符合「连续 N 次丢失 → 视为死亡」的语义。

    该协程通常以 ``asyncio.create_task`` 启动，turn 结束时 ``cancel()``。
    """

    async def _beat() -> None:
        failures = 0
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                with session_scope() as session:
                    ok = renew_lease(session, conversation_id, token, ttl_seconds=ttl_seconds)
                if ok:
                    failures = 0
                    continue
                # 租约已不属于自己（被抢占或过期），停止续租。
                logger.warning(
                    "turn 租约续租失败（已被抢占或丢失），停止续租 conv=%s", conversation_id
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.warning(
                    "turn 租约续租异常（连续第 %d 次）conv=%s",
                    failures,
                    conversation_id,
                    exc_info=True,
                )
                if failures >= max_failures:
                    logger.error(
                        "turn 租约连续 %d 次续租失败，停止续租（租约将自然过期）conv=%s",
                        failures,
                        conversation_id,
                    )
                    return

    return _beat()


__all__ = [
    "acquire_lease",
    "acquire_turn_lease",
    "current_holder",
    "lease_heartbeat",
    "release_lease",
    "release_turn_lease",
    "renew_lease",
]
