"""IM 渠道管理器（单例）—— 支持热重载。

借鉴 OpenClaw 的 channel reload：改凭证后 stop() + start() 只重启那一个渠道，
HTTP 服务和其他状态不动。Bycall 的 IM 渠道在独立后台线程 + 自己的事件循环里跑
（企微 SDK 是 asyncio 的，FastAPI 同步路由在线程池），reload 时安全地换掉那套。

用法：
    mgr = get_channel_manager()
    mgr.start_if_enabled()   # 应用启动时
    mgr.reload()             # admin 改凭证后调用（stop 旧连接 + 用新凭证 start）
    mgr.stop()               # 应用关闭时
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bycall.gateway.base import Channel

logger = logging.getLogger(__name__)


class ChannelManager:
    """管理单个 IM 渠道的生命周期，支持热重载。

    线程模型：渠道在独立 daemon 线程里跑自己的 asyncio 事件循环。
    reload 时先停旧循环（断开连接），再起新循环建新连接。
    """

    def __init__(self) -> None:
        self._channel: Channel | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started and self._channel is not None

    def start_if_enabled(self) -> bool:
        """若 IM 启用且有凭证，启动渠道。返回是否实际启动。"""
        with self._lock:
            if self._started:
                return True
            channel, err = self._build_channel_from_settings()
            if channel is None:
                logger.info("IM 渠道未启动：%s", err)
                return False
            self._channel = channel
            self._start_thread(channel)
            self._started = True
            return True

    def reload(self) -> tuple[bool, str]:
        """热重载：断开旧连接 + 用当前设置建新连接。不重启 HTTP 服务。

        返回 (是否成功, 消息)。无凭证时停止渠道。
        """
        with self._lock:
            was_running = self._started
            # 1. 先停旧的
            if was_running:
                self._stop_locked()
            # 2. 用新设置重建
            channel, err = self._build_channel_from_settings()
            if channel is None:
                self._started = False
                msg = f"IM 渠道已停止（{err}）" if was_running else f"IM 渠道未启动（{err}）"
                logger.info("IM reload: %s", msg)
                return True, msg
            self._channel = channel
            self._start_thread(channel)
            self._started = True
            action = "已重连" if was_running else "已启动"
            return True, f"IM 渠道{action}，使用新凭证"

    def stop(self) -> None:
        """完全停止渠道（应用关闭时调用）。"""
        with self._lock:
            if self._started:
                self._stop_locked()
            self._started = False

    def status(self) -> dict:
        """返回当前状态（供 admin API / 健康检查用）。"""
        return {
            "running": self.is_running,
            "channel": "wecom" if self._channel is not None else None,
        }

    # ---- 内部 ----

    def _build_channel_from_settings(self) -> tuple[Channel | None, str]:
        """从当前 DB/.env 设置构造渠道。无凭证返回 (None, 原因)。"""
        from bycall.gateway.wecom import WecomChannel
        from bycall.governance.settings_store import get_setting_bool, get_setting_value

        if not get_setting_bool("im.enable", default=False, env_var="BYCALL_IM_ENABLE"):
            return None, "IM 未启用（im.enable=false）"

        bot_id = get_setting_value("im.wecom_bot_id", default="", env_var="BYCALL_IM_WECOM_BOT_ID")
        bot_secret = get_setting_value(
            "im.wecom_bot_secret", default="", env_var="BYCALL_IM_WECOM_BOT_SECRET"
        )
        if not bot_id or not bot_secret:
            return None, "缺少 wecom_bot_id 或 wecom_bot_secret"

        return WecomChannel(bot_id=bot_id, secret=bot_secret), ""

    def _start_thread(self, channel: Channel) -> None:
        """在独立 daemon 线程跑渠道的 asyncio 事件循环。"""
        from bycall.agent.im_handler import handle_im_message

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(channel.start(handle_im_message))
                loop.run_forever()
            except Exception:
                logger.exception("IM 渠道事件循环异常")

        t = threading.Thread(target=_run_loop, name="bycall-im", daemon=True)
        self._thread = t
        t.start()
        bot_id_hint = getattr(channel, "_bot_id", "?")
        logger.info("IM 渠道线程已启动 (bot_id=%s)", bot_id_hint)

    def _stop_locked(self) -> None:
        """停止当前渠道（调用方持锁）。"""
        # 1. 断开渠道连接（channel.stop 是同步的，SDK 内部 disconnect）
        if self._channel is not None:
            try:
                self._channel.stop()
            except Exception:
                logger.exception("IM 渠道 stop 异常")
            self._channel = None
        # 2. 停事件循环
        if self._loop is not None and self._loop.is_running():
            import contextlib

            with contextlib.suppress(Exception):
                self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        self._thread = None
        logger.info("IM 渠道已停止（准备重载或关闭）")


# ---- 单例 ----

_manager: ChannelManager | None = None


def get_channel_manager() -> ChannelManager:
    """全局单例。"""
    global _manager
    if _manager is None:
        _manager = ChannelManager()
    return _manager


def reset_channel_manager() -> None:
    """测试用：重置单例。"""
    global _manager
    if _manager is not None:
        _manager.stop()
    _manager = None


__all__ = ["ChannelManager", "get_channel_manager", "reset_channel_manager"]
