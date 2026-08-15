"""发布门（T2.4）：把评测结果接入控制面发布流程。

规则（比 CI 更严，因为这是生产发布前的最后一道闸）：
- **任何配置发布**（prompt/skill/model/retrieval/tool/role）都必须先过门；
- 权限安全用例**全量**必跑（18 条红线级，任一失败即阻断）；
- 与本次变更类型相关的质量/执行用例按 ``change_types`` 追加；
- ``GateReport.passed=False`` 时，发布端点必须返回 409 且不得改动任何 is_active。

发布门与 ``make eval`` 用同一引擎、同一批用例——评测、回放、发布三者口径一致。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from bycall.evals.cases import all_cases, security_cases
from bycall.evals.core import CATEGORY_PERMISSION, EvalContext, EvalReport, run_cases


@dataclass
class GateReport:
    """一次发布门的判定结果（可 JSON 化，落 ReleaseState 供审计）。"""

    kind: str
    passed: bool
    report: EvalReport = field(default_factory=EvalReport)

    @property
    def security_failed(self) -> int:
        return sum(
            1 for r in self.report.results if not r.passed and r.category == CATEGORY_PERMISSION
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "passed": self.passed,
            "security_failed": self.security_failed,
            "report": self.report.to_dict(),
        }


def select_gate_cases(kind: str) -> list:
    """按发布类型选门内用例：全部安全用例 + 与该类型相关的其他类用例。"""
    security = security_cases()
    relevant = [
        c
        for c in all_cases()
        if c.category != CATEGORY_PERMISSION and (not c.change_types or kind in c.change_types)
    ]
    # 去重：安全用例里 change_types 相关的已在 security 中，不再从 all_cases 重复取。
    seen = {c.id for c in security}
    relevant = [c for c in relevant if c.id not in seen]
    return [*security, *relevant]


def run_release_gate(kind: str, *, cases: list | None = None) -> GateReport:
    """离线运行发布门。``cases`` 可注入（测试替身/自定义门内用例）。"""
    selected = list(cases) if cases is not None else select_gate_cases(kind)
    with tempfile.TemporaryDirectory(prefix="bycall-gate-") as tmp:
        report = run_cases(selected, EvalContext(tmp_dir=Path(tmp)))
    return GateReport(kind=kind, passed=report.ok, report=report)


def gate_report_from_json(raw: str) -> GateReport | None:
    """从 ReleaseState.gate_report_json 还原报告（只还原摘要，不还原用例函数）。"""
    import json

    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    report = EvalReport()
    for item in data.get("report", {}).get("results", []):
        from bycall.evals.core import CaseResult

        report.results.append(
            CaseResult(
                case_id=item.get("id", ""),
                name=item.get("name", ""),
                category=item.get("category", ""),
                passed=bool(item.get("passed")),
                error=item.get("error", ""),
                evidence=item.get("evidence", {}),
                duration_ms=float(item.get("duration_ms", 0)),
            )
        )
    return GateReport(kind=data.get("kind", ""), passed=bool(data.get("passed")), report=report)


__all__ = ["GateReport", "gate_report_from_json", "run_release_gate", "select_gate_cases"]
