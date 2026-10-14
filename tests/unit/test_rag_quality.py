"""真实执行契约回归：多库竞争、无答案、故障与最终证据链。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from bycall.agent.context_budget import ContextBudget, budget_and_format_rag
from bycall.llm.base import RankResult
from bycall.rag import retrieve
from bycall.vector_store.models import SearchResult


def hit(id_, text, kb_id=1, score=0.01):
    return SearchResult(id=id_, score=score, text=text, fields={"kb_id": kb_id, "source": "public"})


def test_shared_rerank_selects_relevant_candidates_from_one_kb():
    """多库各自第一名不能挤掉相关库中排名稍后的答案。"""
    relevant = [hit(f"a{i}", f"报价流程答案 {i}") for i in range(5)]
    unrelated = [[hit(f"b{i}", "午餐菜单", kb_id=i + 2)] for i in range(4)]
    pool = retrieve._pool_candidates([relevant, *unrelated], 80)
    model = MagicMock()
    model.rerank.return_value = [
        RankResult(index=i, score=0.9 if r.id.startswith("a") else 0.01) for i, r in enumerate(pool)
    ]
    results = retrieve.rerank_results(model, "报价流程", pool, raise_errors=True)[:5]
    assert [r.id for r in results] == [f"a{i}" for i in range(5)]
    assert all(r.fields["score_kind"] == "rerank" for r in results)


def test_keyword_fallback_rejects_no_answer_and_ranks_all_kbs():
    pool = retrieve._pool_candidates(
        [
            [hit("a0", "报价流程询价签约"), hit("a1", "报价流程需要审批")],
            [hit("b0", "午餐菜单", kb_id=2)],
        ],
        80,
    )
    assert {r.id for r in retrieve._lexical_rank("报价流程", pool)} == {"a0", "a1"}
    assert not retrieve._lexical_rank("维修保养", pool)


@pytest.mark.parametrize(
    "bad", [[], [RankResult(100, 0.9)], [RankResult(0, float("nan"))], [RankResult(0, 2.0)]]
)
def test_malformed_rerank_is_a_failure(bad):
    model = MagicMock()
    model.rerank.return_value = bad
    with pytest.raises(ValueError):
        retrieve.rerank_results(model, "报价", [hit("a", "报价")], raise_errors=True)


def test_retrieval_status_distinguishes_error_no_hit_and_partial(monkeypatch):
    from bycall.llm import resolver
    from bycall.rag import publication
    from bycall.vector_store import factory

    monkeypatch.setattr(publication, "visible_generations", lambda *a: ["doc_1"])
    monkeypatch.setattr(retrieve, "_embed_query", lambda *a: [1.0, 0.0])
    monkeypatch.setattr(resolver, "get_rerank_model", lambda *a, **kw: None)
    stores = [MagicMock(), MagicMock()]
    stores[0].search.side_effect = RuntimeError("磁盘不可用")
    stores[1].search.return_value = [hit("a", "报价流程")]
    monkeypatch.setattr(factory, "create_vector_store", lambda *a, **kw: stores.pop(0))
    specs = [(1, "kb_1", 2, 1), (2, "kb_2", 2, 1)]
    outcome = retrieve.retrieve_multi_kb(MagicMock(), MagicMock(), specs, "报价", ["public"])
    assert outcome.status == "degraded" and outcome
    assert {i["stage"] for i in outcome.issues} == {"search", "rerank"}
    broken = MagicMock()
    broken.search.side_effect = RuntimeError("不可用")
    monkeypatch.setattr(factory, "create_vector_store", lambda *a, **kw: broken)
    outcome = retrieve.retrieve_multi_kb(MagicMock(), MagicMock(), specs[:1], "报价", ["public"])
    assert outcome.status == "error" and not outcome
    broken.search.side_effect = None
    broken.search.return_value = []
    outcome = retrieve.retrieve_multi_kb(MagicMock(), MagicMock(), specs[:1], "报价", ["public"])
    assert outcome.status == "no_hit" and not outcome


def test_final_evidence_only_contains_injected_truncated_text():
    from bycall.agent.runner import _build_citations

    candidates = [hit("a", "报价" * 100), hit("b", "报价" * 100), hit("c", "午餐" * 100)]
    budget = ContextBudget(rag_topk=1, rag_chunk_max_chars=80, rag_context_max_chars=100)
    context, report = budget_and_format_rag(candidates, budget)
    citations = _build_citations(report.evidence)
    assert len(context) <= 100 and len(citations) == 1
    assert citations[0]["snippet"] in context
    assert citations[0]["reference"] == "E1"
    assert citations[0]["chunk_id"] == "a"
    assert candidates[0].text not in context
    assert report.rag_dropped_dedup == 1


def test_rank_scores_are_not_labeled_high_relevance():
    context, report = budget_and_format_rag([hit("a", "片段")], ContextBudget())
    assert "相关性：未校准" in context and "相关性：高" not in context
    assert report.rag_kept_chunks == 1


def test_rewrite_bounded_and_falls_back():
    model = MagicMock()
    model.invoke.return_value = AIMessage(content="报价流程的审批要求")
    history = [SimpleNamespace(role="user", content="报价流程是什么")]
    assert retrieve.rewrite_query(model, "它需要审批吗", history) == "报价流程的审批要求"
    model.invoke.return_value = AIMessage(content="字" * 600)
    assert retrieve.rewrite_query(model, "它需要审批吗", history) == "它需要审批吗"
    model.invoke.side_effect = RuntimeError("不可用")
    assert retrieve.rewrite_query(model, "它需要审批吗", history) == "它需要审批吗"


@pytest.mark.parametrize("streamed", [False, True])
def test_runtime_reports_retrieval_failure_even_if_model_ignores_it(monkeypatch, streamed):
    """同步和流式都由运行时告知检索故障，且公开响应保留同一诊断状态。"""
    import asyncio
    import json

    from bycall.agent import runner
    from bycall.agent.graph import GraphResult
    from bycall.api.dataplane import _to_response
    from bycall.evals.harness import eval_session
    from bycall.governance.capability import CapabilitySet
    from bycall.governance.models import User

    outcome = retrieve.RetrievalOutcome(
        status="error",
        issues=[{"kb_id": 1, "stage": "search", "code": "unavailable"}],
    )
    cap = CapabilitySet(user_id=1, user_name="alice", role="sales", knowledge_scope_tags=["public"])
    monkeypatch.setattr(runner, "compute_capability_set", lambda *a: cap)
    monkeypatch.setattr(runner, "_retrieve_across_kbs", lambda *a: outcome)
    monkeypatch.setattr(runner, "run_tool_loop", lambda **kw: GraphResult(answer="模型给出的回答"))

    async def stream_loop(**kw):
        async def frames():
            yield "模型给出的回答", False, None
            yield "", True, GraphResult(answer="模型给出的回答")

        return frames()

    monkeypatch.setattr(runner, "run_tool_loop_stream", stream_loop)
    with eval_session() as session:
        user = User(name="alice", role="sales")
        session.add(user)
        session.commit()
        args = {"session": session, "user": user, "question": "报价流程", "llm": MagicMock()}
        if streamed:

            async def collect():
                return [frame async for frame in runner.run_agent_stream(**args)]

            frames = asyncio.run(collect())
            result = frames[-1][2]
            assert result.answer == "".join(f[0] for f in frames if not f[1])
        else:
            result = runner.run_agent(**args)
        response = _to_response(result, None)
        assert "知识检索暂不可用" in response.answer
        assert response.retrieval_status == "error"
        assert response.retrieval_diagnostics[0]["code"] == "unavailable"
        assert not response.citations
        trace = json.loads(response.context_trace)
        assert trace["retrieval"]["status"] == "error"
        assert trace["rag_chunks"] == 0


def test_mixed_score_scales_do_not_filter_reranked_evidence():
    """其他模式的分数量纲不能成为重排分数的裁剪阈值。"""
    lexical = hit("lexical", "关键词证据", score=80.0)
    lexical.fields["score_kind"] = "lexical"
    reranked = hit("reranked", "重排证据", score=0.9)
    reranked.fields["score_kind"] = "rerank"
    context, report = budget_and_format_rag([lexical, reranked], ContextBudget())
    assert "重排证据" in context and report.rag_dropped_low_score == 0
