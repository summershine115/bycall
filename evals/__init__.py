"""仓库级评测入口包（薄壳）。

真正的评测引擎在 ``src/bycall/evals/``；本目录提供符合 execution-plan 约定
（``evals/`` 目录 + runner）的 CLI 入口：``python -m evals.run``。
"""

from __future__ import annotations

__version__ = "0.1.0"
