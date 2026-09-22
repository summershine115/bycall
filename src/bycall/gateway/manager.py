"""IM 渠道管理器（单例）—— 多 Bot 槽位 + 热重载（ADR-0012）。

借鉴 OpenClaw 的 channel reload：改凭证后 stop() + start() 只重启涉及的那一个渠道，
HTTP 服务和其他槽位不动。Bycall 的每条 IM 连接在独立后台线程 + 自己的事件循环里跑
（企微 SDK 是 asyncio 的，FastAPI 同步路由在线程池），reload 时安全地换掉那套。

槽位模型（多 Bot 平台，2026-09-22 起 per-Agent 多渠道）：
- ``{渠道}:global``——全局默认 Bot（SystemSetting ``im.*``，当前仅企微），路由到默认
  实例（兜底）；
- ``{渠道}:agent:{id}``——数字员工专属连接（``Agent.im_config`` 按渠道分组、Fernet
  解密），同一实例可同时接入多个不同渠道（每种渠道一条连接），入站消息按连接路由到
  对应实例（会话键带 ``:agent:{id}`` 后缀）；同一凭证（如企微 bot_id）全局只能连一个
  实例，控制面写入前校验拦截。
- ``im.enable=false`` 是 IM 子系统总开关：全局与专属 Bot 都不启动。

用法：
    mgr = get_channel_manager()
    mgr.start_if_enabled()             # 应用启动时（全局 + 启用且有凭证的 Agent）
    mgr.reload()                       # admin 改全局凭证后（只重启全局槽位）
    mgr.reload_agent(agent_id)         # admin 改某实例专属凭证后（只重启该槽位）
    mgr.stop_agent(agent_id)           # 实例禁用/删除时停其专属连接
    mgr.stop()                         # 应用关闭时
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bycall.gateway.base import Channel

logger = logging.getLogger(__name__)

# 槽位 key：全局默认 Bot（{渠道}:global）/ 数字员工专属连接（{渠道}:agent:{id}）。
GLOBAL_SLOT_KEY = "wecom:global"


def global_slot_key(channel_type: str) -> str:
    """全局默认 Bot 的槽位 key（当前仅企微有全局设置）。"""
    return f"{channel_type}:global"


def agent_slot_key(channel_type: str, agent_id: int) -> str:
    """数字员工某渠道专属连接的槽位 key。"""
    return f"{channel_type}:agent:{agent_id}"


class _ChannelSlot:
    """单条 IM 连接的运行时槽位：channel + 线程 + 事件循环 + 生命周期状态。

    状态由 ``WecomChannel`` 生命周期回调（在 IM 事件循环线程被调）经 manager
    ``_on_lifecycle`` 更新，FastAPI 同步路由线程跨线程读 ``status()``。
    """

    def __init__(self, key: str, channel: Channel, agent_id: int | None, bot_id: str) -> None:
        self.key = key
        self.channel = channel
        self.agent_id = agent_id
        self.bot_id = bot_id
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state: dict = {
            "is_connected": False,
            "last_event": None,
            "reconnect_attempts": 0,
            "last_error": "",
        }


class ChannelManager:
    """管理全部 IM 渠道连接的生命周期（多 Bot，ADR-0012），支持按槽位热重载。

    线程模型：每槽位独立 daemon 线程跑自己的 asyncio 事件循环。reload 时先停旧循环
    （断开连接），再起新循环建新连接。
    """

    def __init__(self) -> None:
        self._slots: dict[str, _ChannelSlot] = {}
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._slots)

    def start_if_enabled(self) -> bool:
        """应用启动时启动全部应启动的槽位（全局 + 启用且有专属凭证的 Agent）。

        返回是否至少启动了一个槽位。单个 Agent 槽位失败只记日志，不阻塞其余
        （与 MCPManager.load_all 的"单点失败不阻断"约定一致）。
        """
        with self._lock:
            if self._slots:
                return True
            if not self._im_enabled():
                logger.info("IM 未启用（im.enable=false），跳过全部渠道槽位")
                return False
            started = False
            channel, err = self._build_channel_from_settings()
            if channel is not None:
                self._start_slot_locked(GLOBAL_SLOT_KEY, channel, agent_id=None)
                started = True
            else:
                logger.info("全局 IM 渠道未启动：%s", err)
            for agent_id in self._enabled_agent_ids_with_im():
                for channel_type, agent_channel in self._build_agent_channels(agent_id):
                    key = agent_slot_key(channel_type, agent_id)
                    self._start_slot_locked(key, agent_channel, agent_id=agent_id)
                    started = True
            return started

    def reload(self) -> tuple[bool, str]:
        """热重载全局默认渠道：断开旧连接 + 用当前设置建新连接。不影响 Agent 槽位。

        返回 (是否成功, 消息)。无凭证时停止该槽位。
        """
        with self._lock:
            was_running = GLOBAL_SLOT_KEY in self._slots
            if was_running:
                self._stop_slot_locked(GLOBAL_SLOT_KEY)
            channel, err = self._build_channel_from_settings()
            if channel is None:
                msg = (
                    f"全局 IM 渠道已停止（{err}）"
                    if was_running
                    else f"全局 IM 渠道未启动（{err}）"
                )
                logger.info("IM reload(global): %s", msg)
                return True, msg
            self._start_slot_locked(GLOBAL_SLOT_KEY, channel, agent_id=None)
            action = "已重连" if was_running else "已启动"
            return True, f"全局 IM 渠道{action}，使用新凭证"

    def reload_agent(self, agent_id: int) -> tuple[bool, str]:
        """热重载某数字员工的全部专属渠道连接：断开旧连接 + 用最新 im_config 重建。

        一个实例可同时接入多个渠道（每种渠道一条连接），重载时全部停掉再全部重建。
        无凭证 / 实例禁用或不存在 / IM 未启用时，停掉该实例全部槽位并返回说明。
        """
        with self._lock:
            was_running = any(s.agent_id == agent_id for s in self._slots.values())
            self._stop_agent_slots_locked(agent_id)
            if not self._im_enabled():
                return True, "IM 未启用（im.enable=false），专属连接未启动"
            channels = self._build_agent_channels(agent_id)
            if not channels:
                msg = (
                    f"Agent #{agent_id} 专属连接已全部停止（无凭证/实例禁用或不存在）"
                    if was_running
                    else f"Agent #{agent_id} 无专属渠道凭证，未启动"
                )
                logger.info("IM reload(agent): %s", msg)
                return True, msg
            for channel_type, agent_channel in channels:
                self._start_slot_locked(
                    agent_slot_key(channel_type, agent_id), agent_channel, agent_id=agent_id
                )
            names = "、".join(ch for ch, _ in channels)
            return True, f"Agent #{agent_id} 专属连接已（重）连接（{names}）"

    def stop_agent(self, agent_id: int) -> bool:
        """停止某数字员工的全部专属渠道连接（实例禁用/删除时调用）。返回是否曾运行。"""
        with self._lock:
            was_running = any(s.agent_id == agent_id for s in self._slots.values())
            self._stop_agent_slots_locked(agent_id)
            return was_running

    def _stop_agent_slots_locked(self, agent_id: int) -> None:
        """停掉某实例的全部槽位（调用方持锁）。"""
        for key in [k for k, s in self._slots.items() if s.agent_id == agent_id]:
            self._stop_slot_locked(key)

    def stop(self) -> None:
        """完全停止全部槽位（应用关闭时调用）。"""
        with self._lock:
            for key in list(self._slots):
                self._stop_slot_locked(key)

    def status(self) -> dict:
        """返回全部槽位的连接状态（供 admin API / 健康检查用）。

        顶层字段（running/channel/is_connected/...）沿用全局槽位（无全局时取首个
        槽位）——/ready 等信息性读取的兼容形态；``channels`` 为逐槽位明细列表。
        """
        with self._lock:
            slots = list(self._slots.values())
            channels = [
                {
                    "key": s.key,
                    "channel": s.key.split(":", 1)[0],
                    "scope": "agent" if s.agent_id is not None else "global",
                    "agent_id": s.agent_id,
                    "bot_id": s.bot_id,
                    "is_connected": s.state["is_connected"],
                    "last_event": s.state["last_event"],
                    "reconnect_attempts": s.state["reconnect_attempts"],
                    "last_error": s.state["last_error"],
                }
                for s in slots
            ]
            primary = self._slots.get(GLOBAL_SLOT_KEY) or (slots[0] if slots else None)
            return {
                "running": bool(slots),
                "channel": primary.key.split(":", 1)[0] if primary else None,
                "is_connected": primary.state["is_connected"] if primary else False,
                "last_event": primary.state["last_event"] if primary else None,
                "reconnect_attempts": primary.state["reconnect_attempts"] if primary else 0,
                "last_error": primary.state["last_error"] if primary else "",
                "channels": channels,
            }

    @property
    def is_connected(self) -> bool:
        """真实连接状态（供 /ready 健康检查快速判断）：全局槽位优先，否则任一已连。"""
        with self._lock:
            glob = self._slots.get(GLOBAL_SLOT_KEY)
            if glob is not None:
                return glob.state["is_connected"]
            return any(s.state["is_connected"] for s in self._slots.values())

    # ---- 生命周期状态（WecomChannel 回调，在 IM 事件循环线程被调用）----

    def _on_lifecycle(self, key: str, event: str, **kw: object) -> None:
        """某槽位 WecomChannel 的生命周期回调：更新该槽位的线程安全状态。

        - connected/authenticated：连接可用，清错误；authenticated 复位重连计数。
        - disconnected：连接断开。
        - reconnecting：记录重连次数。
        - error：记录最近错误。
        """
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                return  # 槽位已被 stop 摘除，teardown 期间的事件直接丢弃
            slot.state["last_event"] = event
            if "is_connected" in kw:
                slot.state["is_connected"] = bool(kw["is_connected"])
            if event in ("connected", "authenticated"):
                slot.state["last_error"] = ""
                slot.state["reconnect_attempts"] = 0
            if event == "reconnecting" and "attempt" in kw:
                slot.state["reconnect_attempts"] = int(kw["attempt"])  # type: ignore[arg-type]
            if event == "error" and "error" in kw:
                slot.state["last_error"] = str(kw["error"])

    # ---- 主动推送 ----

    def send_proactive(
        self, chatid: str, body: dict, agent_id: int | None = None, channel: str | None = None
    ) -> bool:
        """尽力主动推送（审批结果/异步任务回推）。**永不向调用方抛异常**
        （审批流程绝不能因推送失败而中断）。

        路由（ADR-0012）：``agent_id`` 指定时优先走该实例的专属连接——chatid 是
        Bot 维度的（哪个 Bot 的会话产生，就要从哪个 Bot 推回去）。一个实例接入多个
        渠道时按 ``channel`` 精确命中对应连接（未指定则取该实例任一连接，行为兜底）。
        把协程 marshal 到对应槽位的 IM 事件循环线程执行，同步等待结果（超时 10s）。
        未运行/未连接/异常 → 记日志 + 返回 False。
        """
        with self._lock:
            slot = None
            if agent_id is not None:
                agent_slots = [s for s in self._slots.values() if s.agent_id == agent_id]
                if channel is not None:
                    slot = next(
                        (s for s in agent_slots if s.key.startswith(f"{channel}:agent:")), None
                    )
                if slot is None:
                    slot = agent_slots[0] if agent_slots else None
            if slot is None:
                slot = self._slots.get(
                    global_slot_key(channel) if channel is not None else GLOBAL_SLOT_KEY
                )
            if slot is None and self._slots:
                # 兜底：未配全局 Bot、仅有专属 Bot 的部署也能推出去。
                slot = next(iter(self._slots.values()))
            channel = slot.channel if slot is not None else None
            loop = slot.loop if slot is not None else None
        if channel is None or loop is None:
            logger.warning("主动推送跳过：无可用 IM 连接，chatid=%s agent_id=%s", chatid, agent_id)
            return False
        if not getattr(channel, "is_connected", False):
            logger.warning("主动推送跳过：IM 连接未就绪，chatid=%s agent_id=%s", chatid, agent_id)
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(channel.send_proactive(chatid, body), loop)
            future.result(timeout=10)
            return True
        except Exception:
            logger.exception("主动推送失败：chatid=%s agent_id=%s", chatid, agent_id)
            return False

    # ---- 内部 ----

    def _im_enabled(self) -> bool:
        """IM 子系统总开关（im.enable，DB > .env）。全局与专属 Bot 共用。"""
        from bycall.governance.settings_store import get_setting_bool

        return get_setting_bool("im.enable", default=False, env_var="BYCALL_IM_ENABLE")

    def _enabled_agent_ids_with_im(self) -> list[int]:
        """全部启用且配置了专属 IM 凭证的 Agent id（启动时的槽位清单来源）。"""
        from bycall.db import session_scope
        from bycall.governance.models import Agent

        try:
            with session_scope() as session:
                rows = session.exec(Agent.select_enabled()).all()
                return [r.id for r in rows if r.im_config_encrypted]  # type: ignore[misc]
        except Exception:
            logger.exception("读取 Agent 专属 Bot 清单失败")
            return []

    def _build_channel_from_settings(self) -> tuple[Channel | None, str]:
        """从当前 DB/.env 设置构造全局默认渠道。无凭证返回 (None, 原因)。"""
        from bycall.governance.settings_store import get_setting_value

        if not self._im_enabled():
            return None, "IM 未启用（im.enable=false）"

        bot_id = get_setting_value("im.wecom_bot_id", default="", env_var="BYCALL_IM_WECOM_BOT_ID")
        bot_secret = get_setting_value(
            "im.wecom_bot_secret", default="", env_var="BYCALL_IM_WECOM_BOT_SECRET"
        )
        if not bot_id or not bot_secret:
            return None, "缺少 wecom_bot_id 或 wecom_secret"
        return self._new_wecom_channel(bot_id, bot_secret, agent_id=None), ""

    def _build_agent_channels(self, agent_id: int) -> list[tuple[str, Channel]]:
        """按 ``Agent.im_config``（解密 + 归一化）构造该实例的全部专属渠道连接。

        一个实例可同时接入多个渠道（2026-09-22 起，ADR-0012 修订）；存量旧扁平格式经
        `normalize_im_config` 归一为按渠道分组。实例不存在/禁用/无凭证/解密失败 →
        空列表（调用方据此跳过或停止槽位）。
        """
        from bycall.db import session_scope
        from bycall.gateway.channels import agent_channels_from_encrypted
        from bycall.governance.models import Agent

        with session_scope() as session:
            agent = session.get(Agent, agent_id)
            if agent is None or not agent.is_enabled or not agent.im_config_encrypted:
                return []
            cfgs = agent_channels_from_encrypted(agent.im_config_encrypted)
        built: list[tuple[str, Channel]] = []
        for channel_type, fields in cfgs.items():
            channel = self._build_channel_for(channel_type, fields, agent_id=agent_id)
            if channel is None:
                logger.warning(
                    "Agent #%s 渠道 %s 无法建连（凭证不完整或未接入连接器），已跳过",
                    agent_id,
                    channel_type,
                )
                continue
            built.append((channel_type, channel))
        return built

    def _build_channel_for(
        self, channel_type: str, fields: dict[str, str], *, agent_id: int
    ) -> Channel | None:
        """按渠道类型构造专属连接（多渠道分派点，当前仅企微）。无法构造返回 None。

        未接入连接器的渠道理论上不会入库（写入侧 `SUPPORTED_CHANNEL_TYPES` 拦截），
        此处再拦一道是纵深防御——历史数据/手工改库不留运行时缝隙。
        """
        if channel_type != "wecom":
            return None
        bot_id = str(fields.get("bot_id") or "").strip()
        secret = str(fields.get("secret") or "").strip()
        if not bot_id or not secret:
            return None
        return self._new_wecom_channel(bot_id, secret, agent_id=agent_id)

    def _new_wecom_channel(self, bot_id: str, secret: str, *, agent_id: int | None) -> Channel:
        """构造 WecomChannel：重连/心跳参数统一从 SystemSetting 读，on_lifecycle 绑定槽位。

        默认 max_reconnect=-1（无限重连，M6：避免渠道连续失败后静默死亡）。
        设置读取失败（如 DB 暂不可用）时退回默认值——建连参数不应让渠道起不来。
        """
        from bycall.gateway.wecom import WecomChannel
        from bycall.governance.settings_store import get_setting_int

        def _int(key: str, default: int, env_var: str) -> int:
            try:
                return get_setting_int(key, default=default, env_var=env_var)
            except Exception:
                return default

        max_reconnect = _int(
            "im.wecom_max_reconnect_attempts",
            -1,
            "BYCALL_IM_WECOM_MAX_RECONNECT_ATTEMPTS",
        )
        reconnect_interval = _int(
            "im.wecom_reconnect_interval_ms",
            1000,
            "BYCALL_IM_WECOM_RECONNECT_INTERVAL_MS",
        )
        heartbeat_interval = _int(
            "im.wecom_heartbeat_interval_ms",
            30000,
            "BYCALL_IM_WECOM_HEARTBEAT_INTERVAL_MS",
        )
        key = GLOBAL_SLOT_KEY if agent_id is None else agent_slot_key("wecom", agent_id)
        return WecomChannel(
            bot_id=bot_id,
            secret=secret,
            max_reconnect_attempts=max_reconnect,
            reconnect_interval_ms=reconnect_interval,
            heartbeat_interval_ms=heartbeat_interval,
            on_lifecycle=lambda event, **kw: self._on_lifecycle(key, event, **kw),
            agent_id=agent_id,
        )

    def _start_slot_locked(self, key: str, channel: Channel, *, agent_id: int | None) -> None:
        """在独立 daemon 线程跑该槽位渠道的 asyncio 事件循环（调用方持锁）。"""
        from bycall.agent.im_handler import handle_im_message

        slot = _ChannelSlot(key, channel, agent_id, bot_id=getattr(channel, "_bot_id", "?"))

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            slot.loop = loop
            try:
                loop.run_until_complete(channel.start(handle_im_message))
                loop.run_forever()
            except Exception:
                logger.exception("IM 渠道事件循环异常 slot=%s", key)

        t = threading.Thread(target=_run_loop, name=f"bycall-im-{key}", daemon=True)
        slot.thread = t
        self._slots[key] = slot
        t.start()
        logger.info("IM 渠道线程已启动 (slot=%s bot_id=%s)", key, slot.bot_id)

    def _stop_slot_locked(self, key: str) -> None:
        """停止指定槽位（调用方持锁）。

        先把槽位从注册表摘除（teardown 期间的残留生命周期事件变为 no-op），再断开。

        关键：必须在 IM 事件循环线程断开连接。本方法在 admin/FastAPI 工作线程被调用
        （无运行中的事件循环），而 SDK 的 ``disconnect()`` 内部用 ``asyncio.ensure_future``
        安排真正的 WS 关闭——在工作线程里会抛 ``no current event loop`` 且 WS 永不关闭。
        旧连接残留后，企微 openws 的单连接策略会让重连后的新连接收不到消息（用户表现为
        「切换 IM 渠道后 Agent 不回复」）。故经 ``run_coroutine_threadsafe`` 把断开 marshal
        到 IM 事件循环线程执行并同步等待真正关闭，再停 loop——保证旧连接彻底断开后再起新连接。
        """
        import contextlib

        slot = self._slots.pop(key, None)
        if slot is None:
            return
        channel, loop = slot.channel, slot.loop
        # 1. 在 IM 事件循环线程断开连接（stop_async 内部调 SDK disconnect，此时 loop 在运行）。
        if channel is not None and loop is not None and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(channel.stop_async(), loop)
                fut.result(timeout=10)
            except Exception:
                logger.exception("IM 渠道断开异常（stop_async，slot=%s）", key)
        # 2. 停事件循环（此时连接已真正关闭，无僵尸连接残留）。
        if loop is not None and loop.is_running():
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        logger.info("IM 槽位已停止（slot=%s）", key)


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
