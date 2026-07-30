"""IM 渠道抽象层。

把各 IM（企微/飞书）的消息收发抽象成统一接口，数据面（agent 逻辑）只依赖
``InboundMessage`` + ``ReplySender``，不感知具体 IM 协议。M2 接飞书时加一个
``FeishuChannel`` 实现即可，业务代码零改动。

抽象要点：
- ``InboundMessage``：各渠道把自家消息格式（企微 frame / 飞书 event）归一成这个值对象。
  ``im_user_id`` 是 IM 侧的可信用户标识（企微是 body.from.userid），数据面据此做身份映射。
  M6 起新增 ``attachments``：携带已解密的图片/文件原始字节（agent 层据此做解析/视觉）。
- ``ReplySender``：回复能力以 callable 注入——``send_text``（单条文本）、可选的
  ``send_stream``（流式，content/stream_id/finish/msg_item）、可选的 ``send_card``（模板卡片）。
  各渠道把自家 SDK 的 reply 方法封到这里，数据面调用时无需知道是企微还是飞书。
- ``Channel``：渠道生命周期。``start`` 启动连接并注册消息回调，``stop`` 关闭；
  ``is_connected`` / ``last_error`` 暴露真实连接状态供健康检查。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    """已解密的入站附件（图片/文件）。

    企微图片/文件消息经 ``download_file`` AES 解密后得到原始字节，各渠道统一封装到这里，
    由 agent 层决定如何处理：文件经 ``rag/parsers/`` 解析，图片可喂视觉模型。本类只负责
    「携带字节」，不做解析（解析逻辑与渠道无关，放渠道层会重复）。
    """

    kind: str  # "image" / "file"
    file_name: str = ""  # 文件名（来自 Content-Disposition，可能为空）
    mime_type: str = ""  # MIME 类型（推断，可能为空）
    content: bytes = b""  # 解密后的原始字节（下载失败时为空）
    text: str = ""  # 文件解析后的文本（图片留空；agent 层解析后回填）


@dataclass
class InboundMessage:
    """统一入站消息。各 IM 渠道把自家消息格式转成这个。"""

    channel: str  # "wecom" / "feishu"
    im_user_id: str  # IM 侧用户标识（企微 body.from.userid）
    text: str  # 文本内容
    raw_frame: Any = None  # 原始帧（回复时需要透传给 SDK，否则无法回包）
    chat_id: str = ""  # 会话标识（群聊 chatid / 单聊留空）
    message_type: str = "text"
    # M6：已解密的图片/文件附件。文本消息为空列表，多模态消息非空。
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class ReplySender:
    """回复能力。各渠道把自家的 reply 封装成 callable 注入。

    - ``send_text``：发一条文本回复（一次性、非流式）。所有渠道必备。
    - ``send_stream``：流式回复，签名 ``async(content, stream_id, finish, msg_item=None)``。
      ``stream_id`` 由调用方生成（同一轮回复复用），``finish=True`` 标记最终帧。
      ``msg_item`` 是图文混排 citation 来源卡片（``[{"title","description","url","picurl"}]``），
      **仅在 ``finish=True`` 的最终帧附带**——中间帧传了也会被 SDK 忽略。
      不支持流式的渠道可留 None，数据面据此降级为单条。
    - ``send_card``：发送一个企微 ``template_card`` dict（非破坏性澄清卡片，如季度选项）。
      与流式文本互不影响、非销毁性，可独立用于澄清式交互。
    - ``send_proactive``：主动推送（审批结果/异步任务回推）。**数据面回复不使用它**
      （per-message sender 恒为 None）；主动推送走 ``ChannelManager.send_proactive``，
      因为它需要跨线程 marshal 到 IM 事件循环，且不依赖某条入站消息的 frame。
    """

    send_text: Callable[[str], Awaitable[None]]
    send_stream: Callable[[str, str, bool, list | None], Awaitable[None]] | None = None
    # 渠道名，方便日志/审计记录回复来源。
    channel: str = ""
    # 透传用的原始帧，部分渠道回复时需要（已封进 callable，留作日志参考）。
    raw_frame: Any = field(default=None, repr=False)
    # M6：发送模板卡片（非破坏性澄清）。
    send_card: Callable[[dict], Awaitable[None]] | None = None
    # M6：主动推送；per-message sender 恒为 None（走 manager）。留作类型完整。
    send_proactive: Callable[[str, dict], Awaitable[None]] | None = None


class Channel(ABC):
    """IM 渠道抽象。``start`` 启动连接，收到消息时回调 handler。

    M6 新增可观测接口：``is_connected`` 反映真实 WebSocket 状态（供 /ready 健康检查），
    ``last_error`` 返回最近一次错误描述（无错为空串）。子类在生命周期回调里维护。
    """

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

    async def stop_async(self) -> None:
        """异步断开连接（在 IM 事件循环线程内执行）。

        默认实现直接调同步 :meth:`stop`——当本方法由 manager 经
        ``run_coroutine_threadsafe`` marshal 到事件循环线程执行时，已有运行中的 loop，
        同步 ``stop()`` 内部对事件循环的依赖（如 ``asyncio.ensure_future``）才能正常工作。
        子类若需要在 loop 线程内等待真正的异步关闭完成，应覆盖此方法。
        """
        self.stop()

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """真实连接状态（子类实现）。"""

    @property
    def last_error(self) -> str:
        """最近一次错误描述（无错为空串）。子类可在错误回调里覆盖。"""
        return ""
