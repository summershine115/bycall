"""RAG 入库管道：PreChunk → embedding → zvec Chunk → 入库。

带 ACL：source 字段 = 知识域标签（检索时按角色 knowledge_scope_tags 过滤）。
父子块支持（M2）：父块 embed 但不参与检索（检索时加 chunk_type=child 过滤），
子块命中后通过 parent_id 解析父块上下文。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from bycall.llm.base import EmbedModel
from bycall.rag.chunking import ChunkingConfig, Document, documents_to_pre_chunks
from bycall.vector_store.base import VectorStore
from bycall.vector_store.models import ACLMetadata, Chunk

logger = logging.getLogger(__name__)

# embedding 单批上限：避免单次请求过多文本导致超时 / 超 token。
_EMBED_BATCH_SIZE = 64

# 默认租户：M1.5 单租户，ACL 的 tenant_id 统一填 default（ABAC 多租户在 M2 启用）。
_DEFAULT_TENANT = "default"


@dataclass
class IngestResult:
    """入库结果。errors 非空不代表整体失败——已写入的 chunk 仍生效。"""

    success: bool
    chunk_count: int
    errors: list[str] = field(default_factory=list)


class IngestPipeline:
    """RAG 入库管道：文档切块 → embedding → 写 zvec → flush。

    一个管道实例绑定一个 collection（默认 ``knowledge``）。
    构造时打开（不存在则按默认 RAG schema 创建，已含 source/doc_id 字段）。

    父子块支持：
    - 父块同样 embed 并写入 zvec，但 chunk_type=parent 标记。
    - 检索时通过 chunk_type=child 过滤，仅返回子块。
    - 子块命中后通过 parent_id 在向量库中查询父块完整内容。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embed_model: EmbedModel,
        collection_name: str = "knowledge",
    ) -> None:
        """打开（或创建）向量库的 knowledge collection。"""
        self._store = vector_store
        self._embed_model = embed_model
        self._store.open(collection_name)

    def ingest_documents(
        self,
        docs: list[Document],
        config: ChunkingConfig | None = None,
    ) -> IngestResult:
        """完整入库：文档→切块→embedding→写 zvec→flush。

        embedding 用 embed_model.embed 批量（分批每批 ≤64 条避免超时）。
        每个 chunk 的 source/doc_id/chunk_type/parent_id/chunk_index 写进 fields，
        id 用 ``{doc_id}_{parent,child}_{idx}``。

        用 ``upsert``（按 id 覆盖写）而非 ``insert``：chunk id 是确定性的
        （``{doc_id}_{idx}``），同一文档 reindex 时 id 不变，upsert 天然幂等——
        即使旧 chunk 未被 delete_by_filter 清掉（zvec 软删除后 insert 会报
        ALREADY_EXISTS），upsert 也能覆盖。

        返回 IngestResult(success, chunk_count, errors)。
        """
        errors: list[str] = []

        # 1. 切块。
        pre_chunks = documents_to_pre_chunks(docs, config=config)
        if not pre_chunks:
            return IngestResult(success=True, chunk_count=0, errors=errors)

        # 2. 分离 parent 和 child chunks。parent 仍需 embed（zvec 要求向量），
        #    但检索时通过 chunk_type=child 过滤排除 parent。
        children = [p for p in pre_chunks if p.chunk_type == "child"]
        parents = [p for p in pre_chunks if p.chunk_type == "parent"]

        # 3. 批量 embedding。对所有 chunk（含 parent）都做 embedding。
        all_pre = children + parents  # children 在前，计数以 children 为主
        vectors: list[list[float]] = []
        for start in range(0, len(all_pre), _EMBED_BATCH_SIZE):
            batch = all_pre[start : start + _EMBED_BATCH_SIZE]
            texts = [p.text for p in batch]
            try:
                batch_vecs = self._embed_model.embed(texts)
                if len(batch_vecs) != len(texts):
                    errors.append(
                        f"embedding 数量不匹配: batch={len(texts)} got={len(batch_vecs)}"
                    )
                    batch_vecs = [[0.0] * _infer_dim(self._store) for _ in texts]
                vectors.extend(batch_vecs)
            except Exception as e:
                logger.warning("embedding 批次失败 (start=%s): %s", start, e)
                errors.append(f"embedding 失败 (start={start}): {e}")
                vectors.extend([[] for _ in texts])

        # 4. 组装 Chunk：source/doc_id/chunk_type/parent_id/chunk_index 进 fields。
        chunks: list[Chunk] = []
        for pre, vec in zip(all_pre, vectors, strict=True):
            if not vec:
                continue
            chunk = Chunk(
                id=pre.id,
                text=pre.text,
                vector=vec,
                fields={
                    "source": pre.source,
                    "doc_id": pre.doc_id,
                    "chunk_type": pre.chunk_type,
                    "chunk_index": pre.chunk_index,
                    "title": pre.title,
                },
                acl=ACLMetadata(tenant_id=_DEFAULT_TENANT),
                parent_id=pre.parent_id,
                chunk_type=pre.chunk_type,
                chunk_index=pre.chunk_index,
            )
            chunks.append(chunk)

        # 5. 写库 + flush。用 upsert 覆盖写（幂等，兼容 reindex + 软删除残留）。
        try:
            results = self._store.upsert(chunks)
            self._store.flush()
        except Exception as e:
            logger.error("入库写库失败: %s", e)
            errors.append(f"写库失败: {e}")
            return IngestResult(
                success=False, chunk_count=len(chunks), errors=errors
            )

        if len(results) != len(chunks):
            errors.append(
                f"写入结果数量不匹配: chunks={len(chunks)} results={len(results)}"
            )
        failed_ids = [r.id for r in results if not r.ok]
        if failed_ids:
            msg = f"{len(failed_ids)} 条 chunk 写入失败: {failed_ids[:10]}"
            logger.warning(msg)
            errors.append(msg)

        # chunk_count 只计子块（父块是内部实现细节）
        child_count = sum(1 for c in chunks if c.chunk_type == "child")
        return IngestResult(
            success=not errors,
            chunk_count=child_count,
            errors=errors,
        )

    def ingest_text(
        self, text: str, source: str, doc_id: str = "",
        config: ChunkingConfig | None = None,
    ) -> IngestResult:
        """便利方法：单段文本入库（自动生成 doc_id）。"""
        if not doc_id:
            doc_id = f"txt_{uuid.uuid4().hex[:12]}"
        return self.ingest_documents(
            [Document(doc_id=doc_id, text=text, source=source)],
            config=config,
        )


def _infer_dim(store: VectorStore) -> int:
    """尽力推断向量维度（embedding 兜底用零向量时需要）。
    因为 VectorStore 接口不暴露 dimension 属性，
    只能在构造函数参数或具体实现上找。
    """
    return getattr(store, "_dimension", 1536) or 1536
