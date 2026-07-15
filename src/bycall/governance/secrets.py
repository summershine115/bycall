"""敏感配置的应用层加密。

数据库只保存带 ``enc:v1:`` 前缀的 Fernet 密文。历史明文记录仍可读取，
管理员下次保存时会自动迁移为密文，避免升级时一次性破坏现有部署。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"


def encrypt_secret(value: str) -> str:
    """加密非空明文；已加密值保持不变。"""
    if not value or value.startswith(_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_secret(value: str) -> str:
    """解密密文；兼容升级前的历史明文。"""
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("敏感配置无法解密，请检查 BYCALL_SECRET_ENCRYPTION_KEY") from exc


def is_encrypted_secret(value: str) -> bool:
    return value.startswith(_PREFIX)


def _fernet() -> Fernet:
    from bycall.config import get_settings

    settings = get_settings()
    raw = settings.secret_encryption_key.get_secret_value() or settings.session_secret
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


__all__ = ["decrypt_secret", "encrypt_secret", "is_encrypted_secret"]
