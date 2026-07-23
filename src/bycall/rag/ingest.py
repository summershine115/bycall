"""RAG 入库管道：PreChunk → embedding → zvec Chunk → 入库。

带 ACL：source 字段 = 知识域标签（检索时按角色 knowledge_scope_tags 过滤）。
父子块支持（M2）：父块 embed 但不参与检索（检索时加 chunk_type=child 过滤），
子块命中后通过 parent_id 解析父块上下文。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
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


def _fire_progress(on_progress: Callable[..., None] | None, **fields: object) -> None:
    """触发进度回调（fire-and-forget）。

    回调自身的异常绝不影响入库主流程——进度是附加观测能力，不能因前端/回调 bug
    导致入库失败。异常吞掉只记 warning。
    """
    if on_progress is None:
        return
    try:
        on_progress(**fields)
    except Exception:
        logger.warning("入库进度回调异常（已忽略，不影响入库）", exc_info=True)


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
        on_progress: Callable[..., None] | None = None,
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
        _fire_progress(on_progress, stage="chunking")
        pre_chunks = documents_to_pre_chunks(docs, config=config)
        if not pre_chunks:
            _fire_progress(on_progress, stage="done")
            return IngestResult(success=True, chunk_count=0, errors=errors)

        # 2. 分离 parent 和 child。parent 不参与向量检索（检索按 chunk_type=child 过滤），
        #    embedding 阶段用零向量占位（见下方循环），不送 provider——parent 尺寸远超模型输入上限。
        children = [p for p in pre_chunks if p.chunk_type == "child"]
        parents = [p for p in pre_chunks if p.chunk_type == "parent"]

        all_pre = children + parents  # children 在前，计数以 children 为主
        # 总量先报给前端（embedding 阶段进度条的分母）。
        _fire_progress(on_progress, stage="embedding", total=len(all_pre))
        vectors: list[list[float]] = []
        processed = 0  # 已向量化 chunk 累计数（含本批，逐步推进 percent）
        for start in range(0, len(all_pre), _EMBED_BATCH_SIZE):
            batch = all_pre[start : start + _EMBED_BATCH_SIZE]
            texts = [p.text for p in batch]
            # parent 块不送 embedding：parent 尺寸（默认 4096 字符 ≈ 1000+ token）远超 embedding
            # 模型输入上限（bge 系列仅 512 token），真 embed 会被 provider 拒收（硅基流动 code 20015）。
            # 检索时按 chunk_type=child 过滤，parent 向量本就不参与召回——用零向量占位满足 zvec schema。
            batch_vecs: list[list[float]] = [
                ([0.0] * self._embed_model.dimension) if p.chunk_type == "parent" else []
                for p in batch
            ]
            # 仅 embed 非空 child：parent 已占位，空/纯空白 child 跳过（bge 拒空白输入，见 chunking._split_adaptive）。
            need_idx = [
                i
                for i, p in enumerate(batch)
                if p.chunk_type != "parent" and p.text and p.text.strip()
            ]
            if need_idx:
                try:
                    got = self._embed_model.embed([texts[i] for i in need_idx])
                    if len(got) != len(need_idx):
                        errors.append(
                            f"embedding 数量不匹配: batch={len(need_idx)} got={len(got)}"
                        )
                        got = [[0.0] * self._embed_model.dimension for _ in need_idx]
                    for i, vec in zip(need_idx, got):
                        batch_vecs[i] = vec
                except Exception as e:
                    logger.warning("embedding 批次失败 (start=%s): %s", start, e)
                    errors.append(f"embedding 失败 (start={start}): {e}")
                    # 失败的 child 位保持 []（下方 `if not vec: continue` 会跳过不入库）
            vectors.extend(batch_vecs)
            # 本批结束推进进度（无论成功/失败，已"处理"这么多）。
            processed += len(texts)
            _fire_progress(on_progress, stage="embedding", processed=processed)

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
                    "parent_id": pre.parent_id or "",
                },
                acl=ACLMetadata(tenant_id=_DEFAULT_TENANT),
                parent_id=pre.parent_id,
                chunk_type=pre.chunk_type,
                chunk_index=pre.chunk_index,
            )
            chunks.append(chunk)

        # 5. 写库 + flush。用 upsert 覆盖写（幂等，兼容 reindex + 软删除残留）。
        _fire_progress(on_progress, stage="indexing")
        try:
            results = self._store.upsert(chunks)
            self._store.flush()
        except Exception as e:
            logger.error("入库写库失败: %s", e)
            errors.append(f"写库失败: {e}")
            return IngestResult(success=False, chunk_count=len(chunks), errors=errors)

        if len(results) != len(chunks):
            errors.append(f"写入结果数量不匹配: chunks={len(chunks)} results={len(results)}")
        failed_ids = [r.id for r in results if not r.ok]
        if failed_ids:
            msg = f"{len(failed_ids)} 条 chunk 写入失败: {failed_ids[:10]}"
            logger.warning(msg)
            errors.append(msg)

        # chunk_count 只计子块（父块是内部实现细节）
        child_count = sum(1 for c in chunks if c.chunk_type == "child")
        # flush 成功即视为入库流程走完（软错误仍按 errors 走 failed，由调用方判定）。
        _fire_progress(on_progress, stage="done")
        return IngestResult(
            success=not errors,
            chunk_count=child_count,
            errors=errors,
        )

    def ingest_text(
        self,
        text: str,
        source: str,
        doc_id: str = "",
        config: ChunkingConfig | None = None,
        on_progress: Callable[..., None] | None = None,
    ) -> IngestResult:
        """便利方法：单段文本入库（自动生成 doc_id）。"""
        if not doc_id:
            doc_id = f"txt_{uuid.uuid4().hex[:12]}"
        return self.ingest_documents(
            [Document(doc_id=doc_id, text=text, source=source)],
            config=config,
            on_progress=on_progress,
        )


def _infer_dim(store: VectorStore) -> int:
    """尽力推断向量维度（embedding 兜底用零向量时需要）。
    因为 VectorStore 接口不暴露 dimension 属性，
    只能在构造函数参数或具体实现上找。
    """
    return getattr(store, "_dimension", 1536) or 1536
