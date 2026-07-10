"""RAG 检索：按用户能力集（knowledge_scope_tags）预过滤检索 + 拼上下文。"""

from __future__ import annotations

import logging

from bycall.llm.base import Message
from bycall.vector_store.base import VectorStore
from bycall.vector_store.models import FilterCondition, SearchRequest, SearchResult

logger = logging.getLogger(__name__)


def retrieve_context(
    store: VectorStore,
    question: str,
    knowledge_scope_tags: list[str],
    collection_name: str = "knowledge",
    topk: int = 5,
) -> list[SearchResult]:
    """按角色知识域预过滤检索。

    - knowledge_scope_tags 含 ``"*"`` → 不过滤（admin 全可见）
    - 否则 → ``FilterCondition(field="source", op="in", values=tags)`` 预过滤
    - 空 tags → 返回空（无可见知识，默认隔离）
    用 full_text 走 FTS 检索（无 query 向量时）。返回 SearchResult 列表。

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
