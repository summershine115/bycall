"""RAG 检索：按用户能力集（knowledge_scope_tags）预过滤检索 + 拼上下文。

检索策略（架构红线 #1：权限预过滤 + 混合检索）：
- vector + full_text 同时给出 → zvec 内部自动挂 RrfReRanker 融合两路（KB 内融合）。
- 跨 KB 合并用 Reciprocal Rank Fusion（按排名融合，不按原始 score），解决不同
  collection / 不同维度下 score 量纲不可比的问题。
- 每个 KB 用各自绑定的 embedding 模型把 query 向量化（per-KB dim），embed 失败则
  该 KB 降级为纯 FTS。
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from bycall.config import VectorStoreConfig
from bycall.llm.base import Message
from bycall.vector_store.base import VectorStore
from bycall.vector_store.models import FilterCondition, SearchRequest, SearchResult

logger = logging.getLogger(__name__)

# RRF 常数：业界标准值（Cormack et al. 2009）。k 越大，头部排名区分度越平缓。
_RRF_K = 60


def retrieve_context(
    store: VectorStore,
    question: str,
    knowledge_scope_tags: list[str],
    query_vector: list[float] | None = None,
    collection_name: str = "knowledge",
    topk: int = 5,
) -> list[SearchResult]:
    """按角色知识域预过滤检索（混合检索：向量 + FTS）。

    - knowledge_scope_tags 含 ``"*"`` → 不过滤（admin 全可见）
    - 否则 → ``FilterCondition(field="source", op="in", values=tags)`` 预过滤
    - 空 tags → 返回空（无可见知识，默认隔离）
    - ``query_vector`` 非空 + ``full_text`` 同时给出 → 混合检索（vector + FTS 两路），
      zvec 内部自动挂 RrfReRanker 融合（KB 内融合）。
    - ``query_vector`` 为 None → 退化为纯 FTS（降级/兼容场景）。

    注意：本函数假设 store 已 open 到目标 collection（``collection_name`` 仅用于
    校验/日志，不重复 open——同一 store 实例一次只能绑一个 collection）。
    """
    # 空 tags：默认隔离，直接返回空，不进检索（不让无权限用户探到知识库内容存在性）。
    if not knowledge_scope_tags:
        return []

    # admin / 通配：不过滤。
    if "*" in knowledge_scope_tags:
        filters: list[FilterCondition] = []
    else:
        filters = [FilterCondition(field="source", op="in", values=knowledge_scope_tags)]

    try:
        results = store.search(
            SearchRequest(
                vector=query_vector,
                full_text=question,
                filters=filters,
                topk=topk,
                output_fields=["text", "source"],
            )
        )
    except Exception as e:
        logger.warning(
            "RAG 检索失败 (collection=%s, tags=%s): %s", collection_name, knowledge_scope_tags, e
        )
        return []
    return results


def retrieve_multi_kb(
    session: Session,
    config: VectorStoreConfig,
    kb_specs: list[tuple[int, str, int, int]],
    question: str,
    knowledge_scope_tags: list[str],
    topk: int = 5,
) -> list[SearchResult]:
    """跨多个 KB collection 检索，per-KB 混合检索后用 RRF 融合取 topk。

    每个 KB：按 ``embedding_model_id`` resolve embed model → embed question 得该 KB 维度
    的 query 向量 → open 该 KB 的 collection → 向量 + FTS 混合检索（zvec 内部 RRF 融合）
    → 收集每个 KB 的 topk 候选 → 跨 KB 用 RRF 按排名融合 → 取最终 topk。

    embed 失败（模型不存在 / 调用异常）→ 该 KB 降级为纯 FTS（记 warning，不中断）。

    为什么用 RRF 而非 score 直排：不同 KB 的 collection 语料规模、词频分布、向量维度
    都可能不同，原始 score 量纲不可比。RRF 只看排名 ``1/(k+rank)``，天然抹平量纲差异，
    是跨打分体系融合的业界标准做法。

    Args:
        session: DB session（按 embedding_model_id 查 Model → resolve embed model）
        config: VectorStoreConfig（读 data_dir / backend）
        kb_specs: [(kb_id, collection_name, dimension, embedding_model_id), ...]
        question: 用户问题
        knowledge_scope_tags: 角色的知识域标签（ACL 预过滤）
        topk: 最终合并后返回的条数

    返回 RRF 融合后的 SearchResult 列表（score 字段被替换为 RRF 融合分）。
    """
    from bycall.vector_store.factory import create_vector_store

    if not knowledge_scope_tags or not kb_specs:
        return []

    per_kb_results: list[list[SearchResult]] = []
    for _kb_id, collection_name, dimension, embedding_model_id in kb_specs:
        try:
            # per-KB embed：用该 KB 绑定的 embedding 模型把 question 向量化。
            query_vector = _embed_query(session, embedding_model_id, question, dimension)

            store = create_vector_store(config, dimension=dimension)
            store.open(collection_name)
            try:
                hits = retrieve_context(
                    store=store,
                    question=question,
                    query_vector=query_vector,
                    knowledge_scope_tags=knowledge_scope_tags,
                    collection_name=collection_name,
                    topk=topk,
                )
                per_kb_results.append(hits)
            finally:
                store.close()
        except Exception as e:
            logger.warning("检索 KB collection %s 失败 (dim=%s): %s", collection_name, dimension, e)

    return _rrf_fuse(per_kb_results, k=_RRF_K, topk=topk)


def _embed_query(
    session: Session, embedding_model_id: int, question: str, dimension: int
) -> list[float] | None:
    """用指定 embedding 模型把 query 文本转向量。

    与入库侧（``admin/api._ingest_document``）镜像同一模式：按 ``embedding_model_id``
    查 Model → ``resolve_embed_model`` → embed。失败返回 None（调用方降级为纯 FTS）。
    """
    from bycall.governance.models import Model
    from bycall.llm.resolver import resolve_embed_model

    entity = session.get(Model, embedding_model_id)
    if entity is None:
        logger.warning("embedding 模型不存在 (id=%s)，该 KB 降级为纯 FTS", embedding_model_id)
        return None
    try:
        embed_model = resolve_embed_model(session, entity)
        vectors = embed_model.embed([question])
        if not vectors:
            return None
        return vectors[0]
    except Exception as e:
        logger.warning(
            "embed query 失败 (model_id=%s, dim=%s)，该 KB 降级为纯 FTS: %s",
            embedding_model_id,
            dimension,
            e,
        )
        return None


def _rrf_fuse(
    per_kb_results: list[list[SearchResult]], *, k: int = _RRF_K, topk: int = 5
) -> list[SearchResult]:
    """Reciprocal Rank Fusion：按排名融合多个 KB 的检索结果。

    公式：``score_rrf(d) = Σ_i 1 / (k + rank_i(d))``，``rank`` 从 1 起计。
    同一个 doc id 在多个 KB 命中时分数累加，因此在多个 KB 都靠前的文档会被加权。

    RRF 只依赖排名，不依赖原始 score 的量纲——这正是它能跨不同打分体系融合的关键
    （不同 KB 的 FTS/BM25 分、不同维度的 cosine 分都不可直接比，但排名可比）。

    融合后 SearchResult 的 ``score`` 字段被替换为 RRF 分（原 score 丢弃，因为不可比）。
    doc 冲突时保留首次出现的那条（text/fields 相同，取任意一条即可）。
    """
    if not per_kb_results:
        return []

    rrf_scores: dict[str, float] = {}
    best: dict[str, SearchResult] = {}

    for hits in per_kb_results:
        for rank, result in enumerate(hits, start=1):
            rrf_scores[result.id] = rrf_scores.get(result.id, 0.0) + 1.0 / (k + rank)
            # 首次出现时记录该 doc 的内容；后续命中只累加分。
            best.setdefault(result.id, result)

    # 按 RRF 分降序，取 topk。返回的 SearchResult 用 RRF 分覆盖原 score。
    ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:topk]
    return [
        SearchResult(
            id=doc_id,
            score=score,
            text=best[doc_id].text,
            fields=best[doc_id].fields,
        )
        for doc_id, score in ranked
    ]


def build_rag_messages(
    system_prompt: str,
    question: str,
    retrieved: list[SearchResult],
) -> list[Message]:
    """拼 RAG prompt：system(控制面提示词) + 知识库上下文 + 用户问题。

    用户问题作为 user message，知识库作为 system context（隔离封装）：
    - 第 1 条 system：控制面激活的系统提示词（角色设定/行为约束）。
    - 第 2 条 system：知识库内容（标注来源 source，明示"均已确认有权访问"）。
    - 第 3 条 user：用户原始问题。
    无检索结果时第二条仍发（内容为空），让 LLM 知道没有可用知识。
    """
    context = "\n\n".join(f"[{r.fields.get('source', '?')}] {r.text}" for r in retrieved)
    return [
        Message(role="system", content=system_prompt),
        Message(
            role="system",
            content=("以下是知识库内容（均已确认你有权访问），请基于它回答：\n" + context),
        ),
        Message(role="user", content=question),
    ]
