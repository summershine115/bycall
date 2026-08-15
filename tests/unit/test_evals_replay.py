"""配置快照 + 离线回放测试（阶段 2 T2.3）。"""

from __future__ import annotations

from bycall.evals.cases import all_cases
from bycall.evals.core import CaseResult, EvalReport
from bycall.evals.harness import eval_session
from bycall.evals.snapshot import (
    ConfigChange,
    ConfigSnapshot,
    capture_config,
    changed_kinds,
    diff_configs,
    diff_verdict,
    select_cases_for_changes,
)


def _snapshots(tmp_path, monkeypatch):
    """抓真实快照 → 手工制造一份「提示词 + 工具白名单」候选快照。"""
    with eval_session() as session:
        base = capture_config(session)
    base_path = tmp_path / "baseline.json"
    base.save(base_path)

    data = base.to_dict()
    data["prompt"] = {
        "version": 2,
        "label": "候选提示词",
        "content_sha256": "b" * 64,
        "is_active": True,
    }
    data["roles"] = {
        "member": {
            "description": "",
            "config": {
                "allowed_tools": ["calculator", "new_tool"],
                "knowledge_scope_tags": ["sales"],
                "level": 0,
                "system_prompt_version": None,
                "skills": [],
                "context_budget": {},
            },
        }
    }
    candidate = ConfigSnapshot.from_dict(data)
    candidate_path = tmp_path / "candidate.json"
    candidate.save(candidate_path)
    return base_path, candidate_path, candidate


def test_capture_and_diff_classifies_change_kinds(tmp_path):
    """diff 把角色字段细分到 tool/retrieval/prompt/skill/role。"""
    base_path, candidate_path, _candidate = _snapshots(tmp_path, None)
    baseline = ConfigSnapshot.load(base_path)
    candidate = ConfigSnapshot.load(candidate_path)
    changes = diff_configs(baseline, candidate)

    assert "prompt" in changed_kinds(changes)
    assert "tool" in changed_kinds(changes)
    assert any(
        c.path.startswith("roles.member.config.allowed_tools") for c in changes if c.kind == "tool"
    )
    assert any(c.path.startswith("roles.member.config.knowledge_scope_tags") for c in changes)


def test_select_cases_for_prompt_change_includes_security_injection():
    """prompt 变更 → 至少选中提示词注入类安全用例。"""
    changes = [ConfigChange(kind="prompt", path="prompt.content", old="a", new="b")]
    selected = select_cases_for_changes(all_cases(), changes)
    ids = {c.id for c in selected}
    assert "security_injection_user_question_stays_user" in ids
    assert "security_injection_rag_stays_user" in ids


def test_diff_verdict_detects_regression():
    """旧跑通过、新跑失败 → 判定失败 + 回退列表。"""
    changes = [ConfigChange(kind="prompt", path="prompt.content", old="a", new="b")]
    base_report = EvalReport(results=[CaseResult(case_id="a", name="a", category="x", passed=True)])
    cand_report = EvalReport(
        results=[CaseResult(case_id="a", name="a", category="x", passed=False)]
    )
    verdict = diff_verdict(base_report, cand_report, changes)
    assert verdict["candidate_failed"] is True
    assert verdict["regressions"] == ["a"]
    assert verdict["verdict"].startswith("失败")


def test_compare_cli_runs_subset_and_writes_artifacts(tmp_path, monkeypatch):
    """--compare：离线回放跑对应子集，写 eval-report.json / eval-diff.md，全绿退出 0。"""
    base_path, candidate_path, _ = _snapshots(tmp_path, None)
    out_dir = tmp_path / "replay"
    from bycall.evals.cli import main

    monkeypatch.chdir(tmp_path)
    exit_code = main(
        ["--compare", str(base_path), str(candidate_path), "--output-dir", str(out_dir)]
    )
    assert exit_code == 0
    assert (out_dir / "eval-report.json").exists()
    assert (out_dir / "eval-diff.md").exists()
    text = (out_dir / "eval-diff.md").read_text(encoding="utf-8")
    assert "配置差异" in text
    assert "通过：未发现越权或质量回退" in text


def test_compare_cli_detects_failing_candidate(tmp_path, monkeypatch):
    """候选评测失败 → 退出码 1（发布门/CI 红灯）。"""
    base_path, candidate_path, _ = _snapshots(tmp_path, None)
    from bycall.evals import core
    from bycall.evals.cli import main

    monkeypatch.chdir(tmp_path)

    # EvalCase 在 import 时已绑定原函数，直接替换模块函数不生效；改在 runner 层
    # 注入失败：候选快照（version=2）跑提示词注入用例时必然失败。
    real_run_case = core.run_case

    def fake_run_case(case, ctx=None):
        if (
            case.id == "security_injection_user_question_stays_user"
            and ctx is not None
            and ctx.candidate is not None
            and ctx.candidate.prompt.get("version") == 2
        ):
            return CaseResult(
                case_id=case.id,
                name=case.name,
                category=case.category,
                passed=False,
                error="候选回归（测试注入）",
            )
        return real_run_case(case, ctx)

    monkeypatch.setattr(core, "run_case", fake_run_case)
    exit_code = main(["--compare", str(base_path), str(candidate_path)])
    assert exit_code == 1
