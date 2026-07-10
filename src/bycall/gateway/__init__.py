"""gateway 包 —— IM 渠道统一层（M1.5 接企微智能机器人）。

把各 IM 的消息收发抽象成统一接口：数据面（agent / /chat 逻辑）只依赖
``InboundMessage`` + ``ReplySender``，不感知具体 IM 协议。

模块组成：
- ``base``：渠道无关的抽象（``Channel`` / ``InboundMessage`` / ``ReplySender``）。
- ``wecom``：企微智能机器人实现（基于 ``wecom-aibot-python-sdk`` 的 WebSocket 长连接）。
- ``identity``：IM 身份 → Bycall ``User`` 映射（``resolve_user_by_im``）。

新增渠道（如飞书，M2）：在 ``base.Channel`` 上实现一个 ``FeishuChannel``，
在 ``identity`` 的渠道名上扩展匹配即可，数据面零改动。

设计约定：
- 凭证从 ``config.IMConfig``（``get_settings().im``）读取，无凭证时 ``enable=False``，
  系统不连 IM，仅靠 Admin 控制台 + /chat 端点测试。
- ``aibot`` SDK 的 import 放在 ``wecom.py`` 的方法内，无凭证/未安装时本包仍可 import，
  ``identity`` 等纯逻辑模块不依赖 SDK。
"""

from __future__ import annotations

from bycall.gateway.base import Channel, InboundMessage, ReplySender
from bycall.gateway.identity import resolve_user_by_im
from bycall.gateway.wecom import WecomChannel

__all__ = [
    "Channel",
    "InboundMessage",
    "ReplySender",
    "WecomChannel",
    "resolve_user_by_im",
]
