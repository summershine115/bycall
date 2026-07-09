"""安全工具：密码哈希、会话 cookie 签名。

直接用 bcrypt 库（不经过 passlib——passlib 与 bcrypt 5.x 有已知兼容问题且已停更）。
bcrypt 限制密码 ≤72 字节，这里在哈希前先做一次 sha256，去掉长度限制。
"""

from __future__ import annotations

import base64
import hashlib

import bcrypt


def hash_password(plain: str) -> str:
    """明文 → bcrypt 哈希（sha256 预处理以绕过 72 字节限制）。返回 str 便于直接存库。"""
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    b64 = base64.b64encode(digest)
    return bcrypt.hashpw(b64, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文是否匹配哈希。"""
    if not hashed:
        return False
    try:
        digest = hashlib.sha256(plain.encode("utf-8")).digest()
        b64 = base64.b64encode(digest)
        return bcrypt.checkpw(b64, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
