"""评测框架核心：用例定义、运行器、计分与报告。

一个用例 = 一段自包含的 Python 校验函数（抛 ``EvalFailure`` 即失败），运行器负责
计时、捕获、归类与汇总。设计目标（execution-plan T2.1）：
- 四类评测集共用同一 runner，结果可量化输出（分数 / 通过率 / 逐类明细）。
- 任一安全用例失败 → 进程以非 0 退出（发布门 / CI 据此红灯）。
- 支持「按变更类型选子集」——用例声明 ``change_types``（prompt/skill/model/
  retrieval/tool/role），T2.3 回放与 T2.4 发布门据此选择相关用例。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bycall.evals.snapshot import ConfigSnapshot

# ============ 类别常量（execution-plan T2.1 四类） ============

CATEGORY_KNOWLEDGE = "knowledge_quality"  # 知识质量
CATEGORY_PERMISSION = "permission_security"  # 权限安全
CATEGORY_EXECUTION = "execution_quality"  # 执行质量
CATEGORY_OPERATIONS = "operations_effectiveness"  # 运营效果

CATEGORY_LABELS = {
    CATEGORY_KNOWLEDGE: "知识质量",
    CATEGORY_PERMISSION: "权限安全",
    CATEGORY_EXECUTION: "执行质量",
    CATEGORY_OPERATIONS: "运营效果",
}

ALL_CATEGORIES = tuple(CATEGORY_LABELS)

# 配置变更类型（与 ``snapshot.diff_configs`` 产出的 kind 对齐）。
CHANGE_KINDS = ("prompt", "skill", "model", "retrieval", "tool", "role")


class EvalFailure(AssertionError):
    """用例失败信号。message 给人看，evidence 给报告/审计看（JSON 安全）。"""

    def __init__(self, message: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence or {}


@dataclass
class EvalContext:
    """一次评测运行的环境上下文。

    ``baseline`` / ``candidate`` 仅在回放（T2.3）时非空；普通运行两者均为 None。
    ``tmp_dir`` 是本次运行的临时目录（zvec 数据等），用例负责在其下创建子目录。
    """

    baseline: ConfigSnapshot | None = None
    candidate: ConfigSnapshot | None = None
    tmp_dir: Path | None = None


@dataclass(frozen=True)
class EvalCase:
    """一条评测用例的静态定义。``run`` 是唯一可变动作。"""

    id: str
    name: str
    category: str
    description: str
    run: Callable[[EvalContext], None]
    # 该用例对哪些配置变更敏感（发布门/回放按此选子集）。
    # 权限安全用例默认全选（任何变更都必须过安全门，见 gate.py）。
    change_types: tuple[str, ...] = ()
    # blocking=失败红灯；warning=失败仅提示（当前所有内置用例均为 blocking）。
    severity: str = "blocking"


@dataclass
class CaseResult:
    """单条用例的运行结果。``evidence`` 只放 JSON 安全值（报告会序列化）。"""

    case_id: str
    name: str
    category: str
    passed: bool
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "name": self.name,
            "category": self.category,
            "passed": self.passed,
            "error": self.error,
            "evidence": self.evidence,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class EvalReport:
    """一次运行的汇总报告。``ok`` 是发布门 / CI / 进程退出码的唯一依据。"""

    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def score(self) -> float:
        """0~1 通过率；无用例按 1.0（空评测集不应阻断发布）。"""
        return self.passed / self.total if self.total else 1.0

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def by_category(self, category: str) -> list[CaseResult]:
        return [r for r in self.results if r.category == category]

    def category_summary(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for category, label in CATEGORY_LABELS.items():
            items = self.by_category(category)
            passed = sum(1 for r in items if r.passed)
            out[category] = {
                "label": label,
                "total": len(items),
                "passed": passed,
                "failed": len(items) - passed,
                "score": passed / len(items) if items else 1.0,
            }
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "score": round(self.score, 4),
            "ok": self.ok,
            "categories": self.category_summary(),
            "results": [r.to_dict() for r in self.results],
        }


def run_case(case: EvalCase, ctx: EvalContext | None = None) -> CaseResult:
    """运行单条用例，把 ``EvalFailure`` / 任意异常都收敛为失败结果。"""
    ctx = ctx or EvalContext()
    start = time.perf_counter()
    try:
        case.run(ctx)
        passed = True
        error = ""
        evidence: dict[str, Any] = {}
    except EvalFailure as exc:
        passed = False
        error = str(exc)
        evidence = exc.evidence
    except Exception as exc:  # 用例内部未捕获异常也按失败计，保证红灯不漏。
        passed = False
        error = f"{type(exc).__name__}: {exc}"
        evidence = {}
    return CaseResult(
        case_id=case.id,
        name=case.name,
        category=case.category,
        passed=passed,
        error=error,
        evidence=evidence,
        duration_ms=(time.perf_counter() - start) * 1000,
    )


def run_cases(
    cases: list[EvalCase],
    ctx: EvalContext | None = None,
    *,
    category: str | None = None,
    categories: list[str] | None = None,
    change_kinds: set[str] | None = None,
) -> EvalReport:
    """运行用例列表，可选按类别 / 变更类型过滤。

    ``change_kinds``：T2.3 回放与发布门用——只保留 ``case.change_types`` 与变更类型
    有交集的用例（``change_types`` 为空的用例视为「任何变更都相关」）。
    """
    selected = list(cases)
    wanted = set(categories or [])
    if category:
        wanted.add(category)
    if wanted:
        selected = [c for c in selected if c.category in wanted]
    if change_kinds is not None:
        selected = [c for c in selected if not c.change_types or set(c.change_types) & change_kinds]
    return EvalReport(results=[run_case(c, ctx) for c in selected])


def json_dump_safe(value: Any) -> Any:
    """把证据值递归转成 JSON 安全结构（dataclass/集合等兜底转 str）。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [json_dump_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_dump_safe(v) for k, v in value.items()}
    return str(value)


def report_to_json(report: EvalReport) -> str:
    """报告序列化（evidence 经 ``json_dump_safe`` 兜底）。"""
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=json_dump_safe)


__all__ = [
    "ALL_CATEGORIES",
    "CATEGORY_EXECUTION",
    "CATEGORY_KNOWLEDGE",
    "CATEGORY_LABELS",
    "CATEGORY_OPERATIONS",
    "CATEGORY_PERMISSION",
    "CaseResult",
    "EvalCase",
    "EvalContext",
    "EvalFailure",
    "EvalReport",
    "json_dump_safe",
    "report_to_json",
    "run_case",
    "run_cases",
]
