"""内置评测用例注册表：四类评测集统一入口。

新增用例只需在对应模块追加 ``EvalCase`` 并加入这里的 CASES 列表拼接，
``make eval`` / 发布门 / 回放自动生效。
"""

from __future__ import annotations

from bycall.evals.cases_execution import CASES as EXECUTION_CASES
from bycall.evals.cases_knowledge import CASES as KNOWLEDGE_CASES
from bycall.evals.cases_operations import CASES as OPERATIONS_CASES
from bycall.evals.cases_security import CASES as SECURITY_CASES
from bycall.evals.core import EvalCase

CASES: list[EvalCase] = [
    *KNOWLEDGE_CASES,
    *SECURITY_CASES,
    *EXECUTION_CASES,
    *OPERATIONS_CASES,
]


def all_cases() -> list[EvalCase]:
    """返回全部内置用例（副本，调用方可自行过滤）。"""
    return list(CASES)


def security_cases() -> list[EvalCase]:
    """权限安全子集（``make eval-security`` / 发布门红线级）。"""
    from bycall.evals.core import CATEGORY_PERMISSION

    return [c for c in CASES if c.category == CATEGORY_PERMISSION]


__all__ = ["CASES", "all_cases", "security_cases"]
