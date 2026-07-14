"""限流器：Redis 后端 + 进程内内存降级。

用于登录端点（防暴力破解）和聊天端点（防费用 DoS）。

设计：
- 优先连 Redis（生产多实例共享计数）。
- 连不上 Redis → 降级进程内 dict（单实例够用，重启清零）。
  ``make ci-local`` 无 Redis 时走内存模式，不抛异常。
- 接口：``allow(key, limit, window_seconds) -> bool``。
  Redis 用 INCR + EXPIRE 原子计数；内存用固定窗口 + 过期清理。

限流维度：
- 登录：``{ip}:{name}``，5 次/15 分钟。
- 聊天：``{user_id}``，30 次/分钟。
"""

from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Literal

logger = logging.getLogger(__name__)

# 限流维度预设。
RATE_LIMIT_LOGIN = {"limit": 5, "window": 900}  # 5 次 / 15 分钟
RATE_LIMIT_CHAT = {"limit": 30, "window": 60}  # 30 次 / 分钟


class RateLimiter:
    """限流器：Redis 优先，连不上降级进程内内存。

    单例（模块级 ``_limiter``），避免每个请求重建 Redis 连接。
    """

    def __init__(self) -> None:
        self._redis = None
        self._mode: Literal["redis", "memory"] = "memory"
        self._memory: dict[str, tuple[int, float]] = {}  # key → (count, window_start)
        self._lock = Lock()
        self._try_connect_redis()

    def _try_connect_redis(self) -> None:
        """尝试连 Redis，成功则切 redis 模式，失败降级 memory 模式。"""
        try:
            import redis

            from bycall.config import get_settings

            url = get_settings().redis.url
            client = redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            self._redis = client
            self._mode = "redis"
            logger.info("限流器已连接 Redis: %s", url)
        except Exception as e:
            self._redis = None
            self._mode = "memory"
            logger.warning("限流器降级为进程内内存模式（Redis 不可用）: %s", e)

    def allow(self, key: str, *, limit: int, window: int) -> bool:
        """检查 key 是否在 limit/window 限额内。

        :return: True=允许，False=超限。
        """
        if self._mode == "redis" and self._redis is not None:
            return self._allow_redis(key, limit, window)
        return self._allow_memory(key, limit, window)

    def _allow_redis(self, key: str, limit: int, window: int) -> bool:
        """Redis 固定窗口：INCR + 首次 EXPIRE。"""
        redis_key = f"ratelimit:{key}"
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.incr(redis_key)
            pipe.expire(redis_key, window, nx=True)  # 仅首次设过期
            count, _ = pipe.execute()
            return count <= limit
        except Exception as e:
            # Redis 运行时故障 → 降级内存（不阻断请求）。
            logger.warning("Redis 限流失败，降级内存: %s", e)
            return self._allow_memory(key, limit, window)

    def _allow_memory(self, key: str, limit: int, window: int) -> bool:
        """进程内固定窗口：dict + 过期清理。"""
        now = time.monotonic()
        with self._lock:
            count, window_start = self._memory.get(key, (0, now))
            # 窗口过期 → 重置。
            if now - window_start >= window:
                count = 0
                window_start = now
            count += 1
            self._memory[key] = (count, window_start)
            # 顺便清理过期 key（惰性，避免开线程）。
            if len(self._memory) > 10000:
                self._purge_expired(now, window)
            return count <= limit

    def _purge_expired(self, now: float, max_window: int) -> None:
        """清理过期计数（防止内存无限增长）。"""
        expired = [k for k, (_, ts) in self._memory.items() if now - ts >= max_window]
        for k in expired:
            del self._memory[k]


# 模块级单例。
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """获取限流器单例。首次调用时尝试连 Redis。"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_limiter() -> None:
    """重置单例（测试用：每个用例后清状态）。"""
    global _limiter
    _limiter = None


__all__ = [
    "RATE_LIMIT_CHAT",
    "RATE_LIMIT_LOGIN",
    "RateLimiter",
    "get_limiter",
    "reset_limiter",
]
