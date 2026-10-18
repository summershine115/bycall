"""共享 pytest fixtures。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from bycall.config import get_settings

# bcrypt 降到 cost=4：每个用例 init_db 都要算一次种子哈希，默认 cost=12 下
# 整套单测纯哈希开销分钟级（哈希格式仍为合法 bcrypt，用例内自洽验证）。
os.environ.setdefault("BYCALL_BCRYPT_ROUNDS", "4")


@pytest.fixture(autouse=True)
def _reset_settings():
    """每个测试用例后清掉 settings 单例缓存，避免跨用例污染。"""
    get_settings.cache_clear()
    # 同步清空工具注册表（M3 工具治理：进程级全局，需跨用例隔离）。
    from bycall.tools.registry import clear_registry

    clear_registry()
    # 压缩改同步（确定性 + 防后台线程挂起 pytest）。
    from bycall.agent import runner as _runner

    _runner._COMPACTION_SYNC = True
    _runner._MEMORY_SYNC = True
    yield
    get_settings.cache_clear()
    clear_registry()
    # 同步重置限流器单例（避免跨用例共享内存计数器）。
    from bycall.governance.ratelimit import reset_limiter

    reset_limiter()
    # 同步清空能力集缓存（M5 性能调优：user.id 跨用例复用，不清会串味）。
    from bycall.governance.capability import clear_capability_cache

    clear_capability_cache()
    _runner._COMPACTION_SYNC = False
    _runner._MEMORY_SYNC = False


@pytest.fixture
def tmp_zvec_dir(tmp_path: Path) -> Path:
    """干净的 zvec 数据目录，用例结束自动清理。"""
    d = tmp_path / "zvec_data"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
