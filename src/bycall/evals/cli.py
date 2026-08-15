"""评测 CLI（execution-plan 阶段 2 出口门）。

用法：
- ``python -m bycall.evals.cli``：跑全部四类评测集；
- ``--security``：只跑权限安全红线用例（= ``make eval-security``）；
- ``--snapshot out.json``：抓当前 DB + 工具注册表配置快照；
- ``--compare old.json new.json``：离线回放——diff 配置 → 按变更类型选子集 →
  跑新旧两份评测 → 输出越权/质量回退判定（T2.3）。
退出码：0 = 通过；1 = 有用例失败；2 = 参数/文件错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from bycall.evals.cases import all_cases, security_cases
from bycall.evals.core import (
    CATEGORY_LABELS,
    EvalContext,
    EvalReport,
    report_to_json,
    run_cases,
)
from bycall.evals.snapshot import (
    ConfigSnapshot,
    describe_changes,
    diff_configs,
    diff_verdict,
    select_cases_for_changes,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bycall-evals", description="Bycall 评测与发布系统")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--security", action="store_true", help="只跑权限安全用例（红线级）")
    mode.add_argument("--list", action="store_true", help="列出全部内置用例")
    mode.add_argument("--snapshot", metavar="PATH", help="抓取当前配置快照并保存")
    mode.add_argument(
        "--compare",
        nargs=2,
        metavar=("OLD", "NEW"),
        help="离线回放：比较两份快照并输出 diff + 判定",
    )
    parser.add_argument(
        "--category", action="append", choices=CATEGORY_LABELS.keys(), help="按类别过滤（可重复）"
    )
    parser.add_argument(
        "--kinds", help="按变更类型过滤用例（逗号分隔，prompt/skill/model/retrieval/tool/role）"
    )
    parser.add_argument("--json", metavar="PATH", help="报告 JSON 输出路径")
    parser.add_argument(
        "--output-dir", metavar="DIR", help="回放产物目录（eval-report.json + eval-diff.md）"
    )
    return parser


def _print_report(report: EvalReport) -> None:
    """人类可读的汇总 + 逐类明细。"""
    print(f"评测结果：{report.passed}/{report.total} 通过（{report.score:.0%}）")
    for _category, summary in report.category_summary().items():
        print(
            f"  {summary['label']}: {summary['passed']}/{summary['total']} 通过"
            + (" ❌" if summary["failed"] else "")
        )
    for result in report.results:
        mark = "✅" if result.passed else "❌"
        print(f"  {mark} [{result.category}] {result.name}（{result.case_id}）")
        if not result.passed:
            print(f"       原因：{result.error}")


def _list_cases() -> None:
    for case in all_cases():
        kinds = ",".join(case.change_types) if case.change_types else "any"
        print(f"{case.id}\t{case.category}\t{kinds}\t{case.name}\t{case.description}")


def _snapshot_command(path: str) -> int:
    """抓取当前 DB 配置快照（复用启动初始化，保证表/seed 就绪）。"""
    from bycall.db import init_db, session_scope
    from bycall.evals.snapshot import capture_config

    init_db()
    with session_scope() as session:
        snapshot = capture_config(session)
    out = snapshot.save(path)
    print(f"配置快照已保存：{out}（prompt={snapshot.prompt.get('version')}）")
    return 0


def _kinds_arg(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {k.strip() for k in raw.split(",") if k.strip()}


def _compare_command(old_path: str, new_path: str, args) -> int:
    """T2.3 离线回放：diff → 选子集 → 跑新旧 → 判定。"""
    baseline = ConfigSnapshot.load(old_path)
    candidate = ConfigSnapshot.load(new_path)
    changes = diff_configs(baseline, candidate)
    print("配置 diff：")
    print(describe_changes(changes) or "  （无差异）")

    selected = select_cases_for_changes(all_cases(), changes)
    if not selected:
        print("按变更类型未匹配到评测子集（无变更或快照仅统计字段变化），判定通过。")
        return 0
    print(f"按变更类型选择评测子集：{len(selected)} 条")

    with tempfile.TemporaryDirectory(prefix="bycall-eval-replay-") as tmp:
        tmp_dir = Path(tmp)
        # 旧/新两侧各用独立子目录：zvec/临时文件绝不能跨侧复用（否则旧侧写入
        # 会污染新侧检索结果，回放结论失真）。
        base_tmp = tmp_dir / "baseline"
        cand_tmp = tmp_dir / "candidate"
        base_tmp.mkdir()
        cand_tmp.mkdir()
        base_report = run_cases(
            selected, EvalContext(baseline=baseline, candidate=baseline, tmp_dir=base_tmp)
        )
        cand_report = run_cases(
            selected, EvalContext(baseline=baseline, candidate=candidate, tmp_dir=cand_tmp)
        )
    verdict = diff_verdict(base_report, cand_report, changes)

    print("\n旧配置结果：")
    _print_report(base_report)
    print("\n新配置结果：")
    _print_report(cand_report)
    print(
        f"\n结论：{verdict['verdict']}；回退={verdict['regressions']}；"
        f"改进={verdict['improvements']}"
    )

    if args.output_dir or args.json:
        out_dir = Path(args.output_dir) if args.output_dir else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "baseline_file": old_path,
            "candidate_file": new_path,
            "verdict": verdict,
            "baseline_report": base_report.to_dict(),
            "candidate_report": cand_report.to_dict(),
        }
        report_path = args.json if args.json else out_dir / "eval-report.json"
        Path(report_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        diff_path = out_dir / "eval-diff.md"
        diff_path.write_text(
            "# 评测回放 diff\n\n"
            f"- baseline: {old_path}\n- candidate: {new_path}\n\n"
            f"## 配置差异\n```\n{describe_changes(changes)}\n```\n\n"
            f"## 判定\n{verdict['verdict']}\n\n"
            f"- 回退用例：{verdict['regressions']}\n- 改进用例：{verdict['improvements']}\n",
            encoding="utf-8",
        )
        print(f"回放产物：{report_path} / {diff_path}")

    return 0 if verdict["verdict"].startswith("通过") else 1


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。返回进程退出码。"""
    args = _build_parser().parse_args(argv)

    if args.list:
        _list_cases()
        return 0

    if args.snapshot:
        return _snapshot_command(args.snapshot)

    if args.compare:
        return _compare_command(args.compare[0], args.compare[1], args)

    # 普通运行：全量或安全子集。
    cases = security_cases() if args.security else all_cases()
    change_kinds = _kinds_arg(args.kinds)
    if args.security and args.category:
        print("--security 与 --category 互斥", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="bycall-eval-") as tmp:
        report = run_cases(
            cases,
            EvalContext(tmp_dir=Path(tmp)),
            categories=args.category,
            change_kinds=change_kinds,
        )

    _print_report(report)
    if args.json:
        Path(args.json).write_text(report_to_json(report), encoding="utf-8")
        print(f"报告已保存：{args.json}")
    if not report.ok:
        print("❌ 评测未通过（任一失败即红灯，发布门将阻断发布）")
        return 1
    print("✅ 评测全部通过")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
