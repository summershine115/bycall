"""``make eval`` 的 CLI 薄壳：把参数原样转给 ``bycall.evals.cli``。"""

from __future__ import annotations

from bycall.evals.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
