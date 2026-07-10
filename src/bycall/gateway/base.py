"""IM 渠道抽象层。

把各 IM（企微/飞书）的消息收发抽象成统一接口，数据面（agent 逻辑）只依赖
``InboundMessage`` + ``ReplySender``，不感知具体 IM 协议。M2 接飞书时加一个
``FeishuChannel`` 实现即可，业务代码零改动。

抽象要点：
- ``InboundMessage``：各渠道把自家消息格式（企微 frame / 飞书 event）归一成这个值对象。
  ``im_user_id`` 是 IM 侧的可信用户标识（企微是 body.from.userid），数据面据此做身份映射。
- ``ReplySender``：回复能力以两个 callable 注入——``send_text``（单条文本）和可选的
  ``send_stream``（流式，content/stream_id/finish）。各渠道把自家 SDK 的 reply 方法封到这里，
  数据面调用时无需知道是企微还是飞书。
- ``Channel``：渠道生命周期。``start`` 启动连接并注册消息回调，``stop`` 关闭。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """统一入站消息。各 IM 渠道把自家消息格式转成这个。"""

    channel: str  # "wecom" / "feishu"
    im_user_id: str  # IM 侧用户标识（企微 body.from.userid）
    text: str  # 文本内容
    raw_frame: Any = None  # 原始帧（回复时需要透传给 SDK，否则无法回包）
    chat_id: str = ""  # 会话标识（群聊 chatid / 单聊留空）
    message_type: str = "text"


@dataclass
class ReplySender:
    """回复能力。各渠道把自家的 reply 封装成 callable 注入。

    - ``send_text``：发一条文本回复（一次性、非流式）。所有渠道必备。
    - ``send_stream``：流式回复，签名 ``async(content, stream_id, finish)``。
      ``stream_id`` 由调用方生成（同一轮回复复用），``finish=True`` 标记最终帧。
      不支持流式的渠道可留 None，数据面据此降级为单条。
    """

    send_text: Callable[[str], Awaitable[None]]
    send_stream: Callable[[str, str, bool], Awaitable[None]] | None = None
    # 渠道名，方便日志/审计记录回复来源。
    channel: str = ""
    # 透传用的原始帧，部分渠道回复时需要（已封进 callable，留作日志参考）。
    raw_frame: Any = field(default=None, repr=False)


class Channel(ABC):
    """IM 渠道抽象。``start`` 启动连接，收到消息时回调 handler。"""

    @abstractmethod
    async def start(
        self,
        on_message: Callable[[InboundMessage, ReplySender], Awaitable[None]],
    ) -> None:
        """启动渠道，注册消息回调。

        ``on_message`` 收到 ``(消息, 回复器)``，异步处理。实现里通常把自家 SDK 的
        事件回调注册成：帧 → ``InboundMessage`` + 构造 ``ReplySender`` → ``await on_message``。
        """

    @abstractmethod
    def stop(self) -> None:
        """停止渠道连接。"""
