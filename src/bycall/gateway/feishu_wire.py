"""飞书长连接协议层（wire format）。

协议无官方公开文档，按官方 ``lark-oapi`` SDK 1.7.3（``lark_oapi/ws/client.py`` +
``ws/pb/pbbp2.proto``）逐字段对齐实现，golden bytes 由官方 SDK 生成锚定
（见 ``tests/unit/test_feishu_gateway.py`` 的 wire 用例）。

为什么自研协议层而不是包官方 SDK：官方 ``ws.Client`` 持有**模块级全局事件循环**且
``start()`` 永久阻塞、无公开停连 API——与 ChannelManager 的多槽位模型
（N 连接 × N 线程 × 各自事件循环，ADR-0012）根本冲突（hermes-agent 单 Bot 前提下
尚需 monkey-patch 全局 loop 才能运行）。详见 ADR-0015。

wire format（proto2，字段号即官方 pbbp2.proto）::

    message Header { required string key = 1; required string value = 2; }
    message Frame {
      required uint64 SeqID           = 1;
      required uint64 LogID           = 2;
      required int32  service         = 3;
      required int32  method          = 4;  // 0=CONTROL, 1=DATA
      repeated Header headers         = 5;  // type/message_id/trace_id/sum/seq/biz_rt
      optional string payload_encoding = 6;
      optional string payload_type     = 7;
      optional bytes  payload          = 8;  // 事件 JSON / Response JSON / 配置 JSON
      optional string LogIDNew         = 9;
    }

只依赖 stdlib + 已有依赖（编解码零依赖；端点发现用 httpx）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace

import httpx

logger = logging.getLogger(__name__)

# ---- 官方常量（lark_oapi/ws/const.py 对齐）----

# 帧类型（Frame.method 字段值）。
FRAME_METHOD_CONTROL = 0
FRAME_METHOD_DATA = 1

# headers["type"] 取值。
MSG_TYPE_PING = "ping"
MSG_TYPE_PONG = "pong"
MSG_TYPE_EVENT = "event"
MSG_TYPE_CARD = "card"

# headers 键名。
HEADER_TYPE = "type"
HEADER_MESSAGE_ID = "message_id"
HEADER_TRACE_ID = "trace_id"
HEADER_SUM = "sum"
HEADER_SEQ = "seq"
HEADER_BIZ_RT = "biz_rt"

# WS 握手失败时错误信息所在 的 upgrade 响应头（对齐官方 const.py）。
HEADER_HANDSHAKE_STATUS = "handshake-status"
HEADER_HANDSHAKE_MSG = "handshake-msg"
HEADER_HANDSHAKE_AUTH_ERRCODE = "handshake-autherrcode"
_AUTH_STATUS_AUTH_FAILED = 514
_AUTH_STATUS_FORBIDDEN = 403
_EXCEED_CONN_LIMIT = 1000040350

# 端点接口业务码。
_CODE_OK = 0
_CODE_SYSTEM_BUSY = 1

# 域名：国内飞书 / 国际 Lark（app 凭证不互通，按部署区域选）。
FEISHU_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"
GEN_ENDPOINT_URI = "/callback/ws/endpoint"

# WS URL query 参数（连接标识 + 应用编号，ping 帧的 service 字段要用）。
QUERY_DEVICE_ID = "device_id"
QUERY_SERVICE_ID = "service_id"


# ---- 异常 ----


class FeishuWireError(Exception):
    """协议层错误基类。"""


class FeishuAuthError(FeishuWireError):
    """凭证错误（端点接口返回非 system-busy 业务码 / 参数缺失）。

    重连无意义——调用方应记录错误并放弃（槽位保留供 stop，is_connected=False）。
    """


class FeishuEndpointError(FeishuWireError):
    """端点获取的瞬态失败（HTTP 非 200 / system busy / 网络异常）——可重试。"""


class FrameDecodeError(FeishuWireError):
    """收到无法解析的帧（不致命：丢帧记日志，连接保持）。"""


# ---- varint / wire 编解码（仅本文件使用的最小实现）----


def _encode_varint(value: int) -> bytes:
    """无符号 varint（protobuf 标准：7bit 一组，小端，最高位续位）。"""
    if value < 0:
        raise ValueError(f"varint 不接受负数: {value}")
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """从 ``data[pos]`` 起读一个 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise FrameDecodeError("varint 越界（帧截断）")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise FrameDecodeError("varint 超长（>10 字节）")


def _put_field(out: bytearray, number: int, wire_type: int, payload: bytes) -> None:
    """写一个字段：tag = (number << 3) | wire_type。"""
    out += _encode_varint((number << 3) | wire_type)
    out += payload


def _put_varint_field(out: bytearray, number: int, value: int) -> None:
    _put_field(out, number, 0, _encode_varint(value))


def _put_bytes_field(out: bytearray, number: int, value: bytes) -> None:
    _put_field(out, number, 2, _encode_varint(len(value)) + value)


# ---- Frame 模型 + 编解码 ----


@dataclass
class Frame:
    """飞书 WS 二进制帧（对应官方 ``pbbp2.Frame``）。

    ``headers`` 用 ``list[tuple[str, str]]`` 保序（回 ACK 时按原序 echo 再追加
    ``biz_rt``，与官方 SDK 行为一致）；``payload`` 为 ``bytes | None`` 以区分
    「未设置」与「空」。
    """

    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = FRAME_METHOD_CONTROL
    headers: list[tuple[str, str]] = field(default_factory=list)
    payload_encoding: str | None = None
    payload_type: str | None = None
    payload: bytes | None = None
    log_id_new: str | None = None

    def header(self, key: str) -> str | None:
        """取首个命中的 header 值（无则 None）。"""
        for k, v in self.headers:
            if k == key:
                return v
        return None


def _encode_header(key: str, value: str) -> bytes:
    """编码单个 Header 子消息（key=1, value=2，均 required）。"""
    out = bytearray()
    kb, vb = key.encode("utf-8"), value.encode("utf-8")
    _put_bytes_field(out, 1, kb)
    _put_bytes_field(out, 2, vb)
    return bytes(out)


def _decode_header(data: bytes) -> tuple[str, str]:
    """解码 Header 子消息。"""
    key = value = ""
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        number, wire_type = tag >> 3, tag & 7
        if wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise FrameDecodeError("Header length-delimited 字段越界")
            raw = data[pos : pos + length]
            pos += length
            if number == 1:
                key = raw.decode("utf-8", errors="replace")
            elif number == 2:
                value = raw.decode("utf-8", errors="replace")
        elif wire_type == 0:
            _, pos = _read_varint(data, pos)
        else:
            raise FrameDecodeError(f"Header 不支持的 wire type: {wire_type}")
    return key, value


def encode_frame(frame: Frame) -> bytes:
    """Frame → bytes（字段升序输出，与官方 SDK 序列化字节一致——golden 锚定）。"""
    out = bytearray()
    _put_varint_field(out, 1, frame.seq_id)
    _put_varint_field(out, 2, frame.log_id)
    _put_varint_field(out, 3, frame.service)
    _put_varint_field(out, 4, frame.method)
    for key, value in frame.headers:
        _put_bytes_field(out, 5, _encode_header(key, value))
    if frame.payload_encoding is not None:
        _put_bytes_field(out, 6, frame.payload_encoding.encode("utf-8"))
    if frame.payload_type is not None:
        _put_bytes_field(out, 7, frame.payload_type.encode("utf-8"))
    if frame.payload is not None:
        _put_bytes_field(out, 8, frame.payload)
    if frame.log_id_new is not None:
        _put_bytes_field(out, 9, frame.log_id_new.encode("utf-8"))
    return bytes(out)


def decode_frame(data: bytes) -> Frame:
    """bytes → Frame。未知字段跳过（前向兼容：服务端加字段不致断连）。"""
    frame = Frame()
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            if number == 1:
                frame.seq_id = value
            elif number == 2:
                frame.log_id = value
            elif number == 3:
                frame.service = value
            elif number == 4:
                frame.method = value
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise FrameDecodeError("Frame length-delimited 字段越界（帧截断）")
            raw = data[pos : pos + length]
            pos += length
            if number == 5:
                frame.headers.append(_decode_header(raw))
            elif number == 6:
                frame.payload_encoding = raw.decode("utf-8", errors="replace")
            elif number == 7:
                frame.payload_type = raw.decode("utf-8", errors="replace")
            elif number == 8:
                frame.payload = raw
            elif number == 9:
                frame.log_id_new = raw.decode("utf-8", errors="replace")
        elif wire_type == 1:  # fixed64：当前 schema 未用，防御性跳过
            pos += 8
        elif wire_type == 5:  # fixed32
            pos += 4
        else:
            raise FrameDecodeError(f"不支持的 wire type: {wire_type}")
    return frame


def ping_frame(service_id: int) -> Frame:
    """构造应用层心跳帧（对齐官方 ``_new_ping_frame``）。"""
    return Frame(
        seq_id=0,
        log_id=0,
        service=service_id,
        method=FRAME_METHOD_CONTROL,
        headers=[(HEADER_TYPE, MSG_TYPE_PING)],
    )


def ack_frame(event_frame: Frame, *, biz_rt_ms: int, code: int = 200) -> Frame:
    """构造事件回执帧：echo 原帧头 + 追加 ``biz_rt``，payload 换成 Response JSON。

    官方协议要求每个事件帧处理后**必须回写**，否则服务端视为未消费而重推。
    payload 与官方 SDK 的 Response 序列化一致（code/headers/data 三字段齐备）。
    """
    headers = [*event_frame.headers, (HEADER_BIZ_RT, str(biz_rt_ms))]
    return replace(
        event_frame,
        headers=headers,
        payload=json.dumps({"code": code, "headers": None, "data": None}).encode("utf-8"),
    )


# ---- 端点发现 + 服务端下发配置 ----


@dataclass
class ClientConfig:
    """服务端下发的连接参数（端点接口返回 + pong 帧可中途更新）。

    与官方 SDK 一致：服务端是权威来源，首次建连后所有重连节奏以最新配置为准。
    """

    reconnect_count: int = -1  # -1 = 无限重连
    reconnect_interval: int = 120  # 秒
    reconnect_nonce: int = 30  # 首次重连的最大随机抖动（秒）
    ping_interval: int = 120  # 应用层心跳间隔（秒）

    @classmethod
    def from_dict(cls, data: dict | None) -> ClientConfig:
        """从端点接口 ``data.ClientConfig`` / pong payload 解析，容忍缺字段。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        for name in ("reconnect_count", "reconnect_interval", "reconnect_nonce", "ping_interval"):
            pascal = "".join(part.capitalize() for part in name.split("_"))
            if isinstance(data.get(pascal), (int, float)):
                setattr(cfg, name, int(data[pascal]))
        return cfg


async def fetch_ws_endpoint(
    client: httpx.AsyncClient, *, domain: str, app_id: str, app_secret: str
) -> tuple[str, ClientConfig]:
    """换取长连接 WSS 端点（对齐官方 ``_get_conn_url``）。

    POST ``{domain}/callback/ws/endpoint``，body ``{"AppID","AppSecret"}``，返回
    ``(wss_url, ClientConfig)``。业务码非 0 且非 system-busy → :class:`FeishuAuthError`
    （凭证/权限问题，重试无意义）；HTTP 异常 / system-busy →
    :class:`FeishuEndpointError`（瞬态，可重试）。
    """
    try:
        resp = await client.post(
            f"{domain}{GEN_ENDPOINT_URI}",
            json={"AppID": app_id, "AppSecret": app_secret},
            headers={"locale": "zh", "User-Agent": "bycall-feishu-gateway/0.3"},
        )
    except httpx.HTTPError as e:
        raise FeishuEndpointError(f"端点接口网络异常: {e}") from e
    if resp.status_code != 200:
        raise FeishuEndpointError(f"端点接口 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise FeishuEndpointError(f"端点接口返回非 JSON: {resp.text[:200]}") from e
    code = int(payload.get("code", -1))
    if code == _CODE_OK:
        data = payload.get("data") or {}
        url = str(data.get("URL") or "")
        if not url:
            raise FeishuAuthError("端点接口未返回 URL（检查应用是否开启长连接模式）")
        return url, ClientConfig.from_dict(data.get("ClientConfig"))
    if code == _CODE_SYSTEM_BUSY:
        raise FeishuEndpointError("端点接口 system busy")
    raise FeishuAuthError(f"端点接口业务错误 code={code} msg={payload.get('msg', '')}")


def parse_conn_params(url: str) -> tuple[str, int]:
    """从 WSS URL query 取 ``(device_id, service_id)``（ping 帧需要 service_id）。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    device_id = (query.get(QUERY_DEVICE_ID) or [""])[0]
    try:
        service_id = int((query.get(QUERY_SERVICE_ID) or ["0"])[0])
    except ValueError:
        service_id = 0
    return device_id, service_id
