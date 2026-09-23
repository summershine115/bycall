"""数字员工头像：本地图片的落盘、识别与站内读取路径。

为什么是「本地图片」而不是外链 URL（2026-09-23 需求）：
- 管理员在控制台直接上传本地图片，不再手贴 ``https://...``——外链会失效、会把
  第三方域名暴露给浏览器，内网部署时常常根本取不到图。
- 文件落 ``settings.avatar_dir``（默认 ``./data/avatars``，与 dev SQLite 同属运行时
  数据根 ``./data/``，已被 .gitignore 排除）。文件名 ``agent_{id}.{ext}``：同一实例
  重复上传直接覆盖，天然不攒孤儿文件。

``Agent.avatar_url`` 列名保持不变（避免动 DB 迁移），语义由「外链」改为**站内路径**
（``/api/admin/agents/{id}/avatar?v=<内容哈希>``）：``v`` 随内容变化，换头像即换 URL，
浏览器缓存不会串；存量外链原值保留，前端 ``<img src>`` 两种形态都能直接渲染。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from bycall.config import get_settings

# 头像大小上限 2MB：列表页逐行渲染，再大没有意义，且防止顺手传原图进内存。
MAX_AVATAR_BYTES = 2 * 1024 * 1024

# 扩展名 → MIME（回吐给浏览器渲染时用）。
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def sniff_image(raw: bytes) -> tuple[str, str] | None:
    """按**文件头魔数**识别图片，返回 ``(扩展名, MIME)``；不认识 → ``None``。

    以内容为准而不是上传者声明的 content-type/文件名——伪造文件名即可绕过声明校验，
    而头像会被原样回吐给浏览器渲染，必须先确认字节真的是图片。

    不收 SVG：它是带脚本能力的文档，放行等于开了个存储型 XSS 口子。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def avatar_dir() -> Path:
    """头像存储目录（懒创建）。"""
    directory = Path(get_settings().avatar_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def avatar_path(agent_id: int) -> Path | None:
    """该实例已落盘的头像文件；没有 → ``None``。"""
    for path in sorted(avatar_dir().glob(f"agent_{agent_id}.*")):
        if path.is_file():
            return path
    return None


def save_avatar(agent_id: int, raw: bytes, ext: str) -> Path:
    """覆盖保存头像。先清同实例旧图，避免换扩展名后残留两张。"""
    remove_avatar(agent_id)
    target = avatar_dir() / f"agent_{agent_id}.{ext}"
    target.write_bytes(raw)
    return target


def remove_avatar(agent_id: int) -> None:
    """删除该实例的全部头像文件（幂等：文件不存在不报错）。"""
    for path in avatar_dir().glob(f"agent_{agent_id}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def avatar_web_url(agent_id: int, raw: bytes) -> str:
    """站内读取路径，带内容哈希版本号——换图即换 URL，浏览器缓存不串。"""
    token = hashlib.sha1(raw).hexdigest()[:12]
    return f"/api/admin/agents/{agent_id}/avatar?v={token}"
