"""渠道类型注册与 per-Agent ``im_config`` 规范化（ADR-0012 修订：多渠道接入）。

背景：ADR-0012 初版把 per-Agent IM 凭证建模为单渠道（企微）扁平 dict
``{"wecom_bot_id": ..., "wecom_secret": ...}``。2026-09-22 起泛化为**按渠道分组**：

    {"wecom": {"bot_id": "...", "secret": "..."}, "feishu": {...}}

约束（本模块是唯一权威）：
- 同一数字员工可接入**多个不同渠道**（每种渠道一条专属连接）；
- **同一凭证只能连接一个数字员工**——同一渠道内的凭证身份字段（如企微 bot_id）
  跨 Agent、以及与全局默认 Bot 不得冲突（写入前由控制面校验，运行时企微单连接
  策略也会互踢，必须在写入前拦住）；
- 只有已接入连接器的渠道类型可配置（见 ``SUPPORTED_CHANNEL_TYPES``），避免
  「配置了看似生效、实际没有连接」的治理缝隙。

兼容：存量库里的旧扁平格式在读取时经 :func:`normalize_im_config` 归一为新格式
（懒升级——下次写入落库的即是新格式，无需 DB 迁移）。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 已接入连接器、可配置 per-Agent 专属凭证的渠道类型。
# feishu 等待 FeishuChannel 连接器落地后再加入（渠道接入页有占位卡）。
SUPPORTED_CHANNEL_TYPES: frozenset[str] = frozenset({"wecom"})

# 各渠道凭证里的「身份字段」：同一渠道内该字段全局唯一（跨 Agent / 与全局默认）。
# 全局默认 Bot 的 SystemSetting 键约定为 ``im.{渠道}_{身份字段}``（如 im.wecom_bot_id）。
CREDENTIAL_ID_FIELDS: dict[str, str] = {
    "wecom": "bot_id",
}

# 各渠道专属凭证的必填字段（写入前校验：配置了半套凭证的渠道不会生效，直接拦）。
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "wecom": ("bot_id", "secret"),
}

# 旧版扁平 im_config 字段 → (渠道类型, 新字段名)。读取兼容用，写入一律新格式。
_LEGACY_FIELDS: dict[str, tuple[str, str]] = {
    "wecom_bot_id": ("wecom", "bot_id"),
    "wecom_secret": ("wecom", "secret"),
}


def normalize_im_config(cfg: dict | None) -> dict[str, dict[str, str]]:
    """把任意历史形态的 im_config 归一为 ``{渠道: {字段: 值}}``。

    - 旧扁平格式（顶层 ``wecom_bot_id``/``wecom_secret``）→ ``{"wecom": {...}}``；
    - 新分组格式 → 原样保留（值转 str，去首尾空白）；
    - 字段全为空的渠道条目直接丢弃（等价于未配置该渠道）。
    """
    normalized: dict[str, dict[str, str]] = {}
    if not isinstance(cfg, dict):
        return normalized
    for key, value in cfg.items():
        if key in _LEGACY_FIELDS:
            ch, field = _LEGACY_FIELDS[key]
            normalized.setdefault(ch, {})[field] = str(value or "").strip()
        elif isinstance(key, str) and isinstance(value, dict):
            fields = {str(k): str(v or "").strip() for k, v in value.items() if v is not None}
            if fields:
                normalized[key] = fields
    return {ch: f for ch, f in normalized.items() if any(f.values())}


def agent_channels_from_encrypted(encrypted: str | None) -> dict[str, dict[str, str]]:
    """解密 ``Agent.im_config_encrypted`` 并归一为按渠道分组 dict。

    解密/解析失败返回 ``{}``（他人凭证损坏不应阻断本次读写，运行时加载时另行报错）。
    """
    if not encrypted:
        return {}
    from bycall.governance.secrets import decrypt_secret

    try:
        raw = decrypt_secret(encrypted)
        return normalize_im_config(json.loads(raw) if raw else {})
    except Exception:
        logger.exception("im_config 解密/解析失败，按未配置处理")
        return {}


def credential_id(channel_type: str, fields: dict[str, str] | None) -> str:
    """取某渠道配置里的凭证身份字段值（企微为 bot_id）。未配置返回空串。"""
    if not fields:
        return ""
    return str(fields.get(CREDENTIAL_ID_FIELDS.get(channel_type, ""), "") or "").strip()


def global_credential_setting_key(channel_type: str) -> str:
    """全局默认 Bot 的凭证身份 SystemSetting 键（如 ``im.wecom_bot_id``）。"""
    return f"im.{channel_type}_{CREDENTIAL_ID_FIELDS[channel_type]}"


def parse_im_config_patch(raw: dict | None) -> dict[str, dict[str, str]]:
    """解析控制面 PATCH 载荷里的 im_config（与 :func:`normalize_im_config` 语义不同）。

    PATCH 语义必须区分「未提及」与「移除」，不能丢弃空渠道条目：
    - ``{"wecom": {"bot_id": ...}}`` → 设置/替换 wecom 渠道；
    - ``{"wecom": {}}`` → **移除** wecom 渠道（空 dict 是显式移除信号）；
    - 旧扁平字段（​wecom_bot_id 等）兼容归一到 wecom 渠道；
    - 顶层出现非 dict 值（既不是渠道分组也不是旧扁平字段）→ ValueError（调用方转 400），
      避免拼写错的渠道名被静默吞掉。
    """
    patch: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        raise ValueError("im_config 必须是按渠道分组的对象")
    for key, value in raw.items():
        if key in _LEGACY_FIELDS:
            ch, field = _LEGACY_FIELDS[key]
            patch.setdefault(ch, {})[field] = str(value or "").strip()
        elif isinstance(key, str) and isinstance(value, dict):
            patch[key] = {str(k): str(v or "").strip() for k, v in value.items()}
        else:
            raise ValueError(f"im_config 字段 '{key}' 不是渠道分组（应为 {{字段: 值}} 对象）")
    return patch


def merge_im_config(
    current: dict[str, dict[str, str]], patch: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    """按渠道合并更新（控制面 PATCH 语义的纯函数）。

    - ``patch`` 为空 dict → 清空全部渠道（沿用旧「{}=清空」语义）；
    - ``patch`` 里某渠道字段非空 → 整体替换该渠道（其他渠道不动）；
    - ``patch`` 里某渠道字段为空（``{"wecom": {}}``）→ 移除该渠道。
    """
    if not patch:
        return {}
    merged = {ch: dict(fields) for ch, fields in current.items()}
    for ch, fields in patch.items():
        if any(fields.values()):
            merged[ch] = dict(fields)
        else:
            merged.pop(ch, None)
    return merged


__all__ = [
    "CREDENTIAL_ID_FIELDS",
    "REQUIRED_FIELDS",
    "SUPPORTED_CHANNEL_TYPES",
    "agent_channels_from_encrypted",
    "credential_id",
    "global_credential_setting_key",
    "merge_im_config",
    "normalize_im_config",
    "parse_im_config_patch",
]
