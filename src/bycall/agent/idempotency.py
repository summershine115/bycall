"""入站消息幂等去重——同一条 IM 消息只执行一次（参考 QM ``IdempotencyStore``）。

企微长连接重连或事件重试会让同一条消息（同一 ``MsgId``）重复投递。若不去重，重复
消息会触发两次完整的治理链路（两次 RAG + LLM + 审计），产生双回答 / 双费用 / 双审计。

两层去重（缺一不可）：

1. **进程内 inflight 去重**：同一条消息正在处理中时，第二条直接丢弃。IM 渠道是单事件
   循环，但重连重发可能让同一 ``MsgId`` 在前一条尚未处理完时再次到达——inflight 集合
   保证「正在处理」的键不会被二次进入。
2. **DB 持久化去重**：处理完毕的键落 ``IdempotencyRecord`` 表。即使进程重启、inflight
   集合丢失，已处理过的消息也不会被重复执行。

时序无锁洞：先落 DB done 记录，再清 inflight（见 :func:`mark_message_done` 与
:func:`release_inflight` 的调用约定），确保 inflight 与 done 记录的防护区间无缝衔接，
不存在「两者皆空」的窗口让重复消息漏过。

retention：``prune_idempotency_records`` 删除超期记录（默认 14 天），避免表无限增长；
``claim_message`` 以低概率触发一次清理（懒清理，无需独立定时任务）。
"""

from __future__ import annotations

import logging
import random
import threading

from sqlmodel import Session

from bycall.config import get_settings
from bycall.governance.models import IdempotencyRecord

logger = logging.getLogger(__name__)

# 进程内「正在处理」的幂等键集合。IM 是单事件循环，但为防御多线程/多事件循环场景，
# 用锁保护集合的「检查 + 加入」原子性（避免两路同时通过检查再各自加入）。
_inflight: set[str] = set()
_lock = threading.Lock()

# 懒清理概率：每处理一条消息有 1/_PRUNE_INTERVAL 的概率顺带清理过期幂等记录。
# 概率式清理在高频时足够频繁、低频时表本就不增长，无需独立定时任务。
_PRUNE_INTERVAL = 128


def make_key(channel: str, msg_id: str) -> str | None:
    """构造幂等键。无 msg_id（如部分事件回执）时返回 None——无法去重，调用方应放行。"""
    if not msg_id:
        return None
    return f"{channel}:{msg_id}"


def claim_message(session: Session, key: str) -> bool:
    """原子认领一条消息：返回 True 表示首次到达（继续处理），False 表示重复（丢弃）。

    同时做 inflight 去重与 DB done 去重；两者都在锁内完成，保证「检查 + 标记 inflight」
    不可分割（不会有两路同时通过检查）。命中任意一层都直接返回 False。
    """
    with _lock:
        if key in _inflight:
            from bycall.observability.metrics import DUPLICATE_MESSAGES

            DUPLICATE_MESSAGES.labels(layer="inflight").inc()
            return False
        existing = session.exec(IdempotencyRecord.select_by_key(key)).first()
        if existing is not None:
            from bycall.observability.metrics import DUPLICATE_MESSAGES

            DUPLICATE_MESSAGES.labels(layer="persisted").inc()
            return False
        _inflight.add(key)

    # 懒清理：低概率顺带删过期记录（在调用方的事务里提交）。
    if random.randint(1, _PRUNE_INTERVAL) == 1:
        try:
            prune_idempotency_records(session)
        except Exception:
            logger.debug("幂等记录懒清理失败（忽略）", exc_info=True)
    return True


def mark_message_done(session: Session, key: str) -> None:
    """标记一条消息已处理完毕——落 ``IdempotencyRecord``。

    调用约定：**必须在 :func:`release_inflight` 之前调用**。这样 done 记录先于 inflight
    清除写入，防护区间无缝衔接，避免重复消息在「inflight 已清、done 未写」的窗口漏过。
    重复落库（理论上不应发生，inflight 已防住）按幂等处理——不报错。
    """
    existing = session.exec(IdempotencyRecord.select_by_key(key)).first()
    if existing is not None:
        return
    session.add(IdempotencyRecord(key=key))


def release_inflight(key: str) -> None:
    """清除 inflight 标记（消息处理结束）。

    应在 :func:`mark_message_done` 之后调用；瞬态拒绝（如限流）未落 done 记录时也调用本函数
    单独清 inflight，使重发可重新尝试。
    """
    with _lock:
        _inflight.discard(key)


def prune_idempotency_records(session: Session, *, retention_days: int | None = None) -> int:
    """删除超期的幂等记录，返回删除条数。避免表随消息量无限增长。"""
    from datetime import UTC, datetime, timedelta

    from sqlmodel import delete

    if retention_days is None:
        retention_days = get_settings().idempotency_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = session.exec(delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff))
    deleted = getattr(result, "rowcount", 0) or 0
    if deleted:
        logger.info("清理过期幂等记录 %d 条（retention=%d 天）", deleted, retention_days)
    return deleted


__all__ = [
    "claim_message",
    "make_key",
    "mark_message_done",
    "prune_idempotency_records",
    "release_inflight",
]
