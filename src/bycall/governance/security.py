"""安全工具：密码哈希、会话 cookie 签名。

直接用 bcrypt 库（不经过 passlib——passlib 与 bcrypt 5.x 有已知兼容问题且已停更）。
bcrypt 限制密码 ≤72 字节，这里在哈希前先做一次 sha256，去掉长度限制。
"""

from __future__ import annotations

import base64
import hashlib
import os

import bcrypt

# bcrypt cost。默认 12（生产强度，单次 ~300ms）；仅测试通过环境变量降到 4——
# 每个用例的 init_db 种子都要现算 admin 哈希，cost=12 时整套单测纯哈希开销达分钟级。
_BCRYPT_ROUNDS_ENV = "BYCALL_BCRYPT_ROUNDS"


def _bcrypt_rounds() -> int:
    try:
        return max(4, int(os.environ.get(_BCRYPT_ROUNDS_ENV, "12")))
    except ValueError:
        return 12


def hash_password(plain: str) -> str:
    """明文 → bcrypt 哈希（sha256 预处理以绕过 72 字节限制）。返回 str 便于直接存库。"""
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    b64 = base64.b64encode(digest)
    return bcrypt.hashpw(b64, bcrypt.gensalt(rounds=_bcrypt_rounds())).decode("utf-8")


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
