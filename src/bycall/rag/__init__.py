"""RAG（检索增强生成）入库与检索管道。

三个阶段，各自独立可测：
- ``chunking``：原始文档 → chunk 文本列表（带文档级 ACL 元数据 source 标签）。
- ``ingest``：PreChunk → embedding → zvec Chunk → 入库（source 字段 = 知识域标签）。
- ``retrieve``：按用户能力集（knowledge_scope_tags）预过滤检索 + 拼上下文 message。

权限模型：每个 chunk 的 ``source`` 字段是知识域标签，对应角色的
``knowledge_scope_tags``；检索时把它翻译成 ``source IN (...)`` 预过滤，
admin（``['*']``）全可见，空标签默认隔离（返回空）。入库由 admin 在控制面管理。

依赖最小：不引入 langchain 的 splitter，切块用纯 Python（段落优先 + 超长二次切）。
"""
from bycall.rag.chunking import (
    ChunkingConfig,
    Document,
    PreChunk,
    Section,
    documents_to_pre_chunks,
    split_document,
    split_parent_child,
)
from bycall.rag.ingest import IngestPipeline, IngestResult
from bycall.rag.retrieve import (
    build_rag_messages,
    rerank_results,
    retrieve_context,
    retrieve_multi_kb,
    rewrite_query,
)

__all__ = [
    "ChunkingConfig",
    "Document",
    "IngestPipeline",
    "IngestResult",
    "PreChunk",
    "Section",
    "build_rag_messages",
    "documents_to_pre_chunks",
    "rerank_results",
    "retrieve_context",
    "retrieve_multi_kb",
    "rewrite_query",
    "split_document",
    "split_parent_child",
]
