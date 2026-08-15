"""评测引擎单元测试（阶段 2 T2.1/T2.2）。

验证：四类内置用例离线全绿、安全用例数量达标（五类攻击 × ≥3）、
失败收敛为红灯、按变更类型选子集、CLI 退出码语义。
"""

from __future__ import annotations

from bycall.evals.cases import all_cases, security_cases
from bycall.evals.core import (
    CATEGORY_EXECUTION,
    CATEGORY_KNOWLEDGE,
    CATEGORY_OPERATIONS,
    CATEGORY_PERMISSION,
    CaseResult,
    EvalCase,
    EvalContext,
    EvalFailure,
    EvalReport,
    run_case,
    run_cases,
)


def test_all_builtin_cases_pass_offline(tmp_path):
    """make eval 的全部内置用例离线可跑且全绿（任一失败 = 红灯）。"""
    report = run_cases(all_cases(), EvalContext(tmp_dir=tmp_path))
    assert report.ok, [r.error for r in report.results if not r.passed]
    assert report.total >= 20  # 四类骨架各至少 1 条样例
    assert {
        CATEGORY_KNOWLEDGE,
        CATEGORY_PERMISSION,
        CATEGORY_EXECUTION,
        CATEGORY_OPERATIONS,
    } <= set(report.category_summary())


def test_security_five_attack_categories_each_at_least_three():
    """T2.2 红线级：五类攻击各 ≥3 条，全部 blocking。"""
    cases = security_cases()
    by_prefix = {}
    for case in cases:
        assert case.category == CATEGORY_PERMISSION
        assert case.severity == "blocking"
        # 用例 id 约定：security_<攻击类>_<序号>，取前两段作为攻击类。
        group = "_".join(case.id.split("_")[:2])
        by_prefix.setdefault(group, []).append(case.id)
    for group, ids in by_prefix.items():
        assert len(ids) >= 3, f"{group} 攻击用例不足 3 条: {ids}"
    assert len(by_prefix) >= 5  # 越权召回/记忆泄漏/提示词注入/工具越权/审批绕过


def test_failure_raises_red_light(tmp_path):
    """任一失败用例即红灯（report.ok=False），且失败原因被收敛。"""

    def boom(_ctx):
        raise EvalFailure("模拟越权", {"unauthorized": True})

    case = EvalCase(
        id="x_fail",
        name="必然失败",
        category=CATEGORY_PERMISSION,
        description="失败收敛测试",
        run=boom,
    )
    report = run_cases([case], EvalContext(tmp_dir=tmp_path))
    assert not report.ok
    assert report.failed == 1
    assert report.results[0].error == "模拟越权"
    assert report.results[0].evidence == {"unauthorized": True}


def test_unexpected_exception_also_fails():
    """用例内部未捕获异常同样按失败计，红灯不漏。"""

    def broken(_ctx):
        raise RuntimeError("boom")

    result = run_case(
        EvalCase(
            id="x_broken",
            name="异常用例",
            category=CATEGORY_EXECUTION,
            description="",
            run=broken,
        )
    )
    assert not result.passed
    assert "RuntimeError" in result.error


def test_change_kinds_subset_selection():
    """T2.3/T2.4：按变更类型选择评测子集。"""
    cases = [
        EvalCase(
            id="a",
            name="a",
            category=CATEGORY_EXECUTION,
            description="",
            run=lambda c: None,
            change_types=("prompt",),
        ),
        EvalCase(
            id="b",
            name="b",
            category=CATEGORY_EXECUTION,
            description="",
            run=lambda c: None,
            change_types=("tool",),
        ),
        EvalCase(
            id="c",
            name="c",
            category=CATEGORY_EXECUTION,
            description="",
            run=lambda c: None,
            change_types=(),
        ),
    ]
    report = run_cases(cases, change_kinds={"prompt"})
    assert {r.case_id for r in report.results} == {"a", "c"}
    report2 = run_cases(cases, change_kinds={"retrieval"})
    assert {r.case_id for r in report2.results} == {"c"}  # 空 change_types = 任何变更都相关


def test_category_filter_and_score():
    """按类别过滤 + 可量化分数。"""
    cases = [
        EvalCase(id="k", name="k", category=CATEGORY_KNOWLEDGE, description="", run=lambda c: None),
        EvalCase(id="e", name="e", category=CATEGORY_EXECUTION, description="", run=lambda c: None),
    ]
    report = run_cases(cases, category=CATEGORY_KNOWLEDGE)
    assert report.total == 1
    assert report.score == 1.0
    assert report.to_dict()["categories"][CATEGORY_KNOWLEDGE]["total"] == 1


def test_empty_report_ok_for_empty_subset():
    """无匹配用例的空子集不阻断（发布门按「无相关用例」放行）。"""
    assert EvalReport().ok
    assert EvalReport().score == 1.0


def test_case_result_json_safe():
    """报告可 JSON 序列化（evidence 兜底转换）。"""
    from bycall.evals.core import report_to_json

    report = EvalReport(
        results=[
            CaseResult(
                case_id="a",
                name="a",
                category=CATEGORY_EXECUTION,
                passed=True,
                evidence={"set": {1, 2}},
            )
        ]
    )
    assert '"set"' in report_to_json(report)


def test_cli_list_and_security_exit(tmp_path, monkeypatch):
    """CLI：--list 返回 0；--security 全绿返回 0。"""
    from bycall.evals.cli import main

    assert main(["--list"]) == 0
    monkeypatch.chdir(tmp_path)  # 避免真实 cwd 下的快照/报告产物
    assert main(["--security", "--json", str(tmp_path / "sec.json")]) == 0
    assert (tmp_path / "sec.json").exists()
