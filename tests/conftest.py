"""共享 pytest fixtures。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bycall.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings():
    """每个测试用例后清掉 settings 单例缓存，避免跨用例污染。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    # 同步重置限流器单例（避免跨用例共享内存计数器）。
    from bycall.governance.ratelimit import reset_limiter

    reset_limiter()


@pytest.fixture
def tmp_zvec_dir(tmp_path: Path) -> Path:
    """干净的 zvec 数据目录，用例结束自动清理。"""
    d = tmp_path / "zvec_data"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
