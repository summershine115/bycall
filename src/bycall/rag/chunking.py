"""文档切块：原始文档 → Chunk 列表。

M1.5 用简单按段落/句子切（不引入 langchain 的 splitter，保持依赖最小）。
每个 chunk 带文档级的 ACL 元数据（source 标签 = 知识域，对应角色 knowledge_scope_tags）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Document:
    """待入库的原始文档。"""

    doc_id: str  # 文档唯一 id
    text: str  # 正文
    source: str  # 知识域标签，如 "public"/"sales"/"hr"（对应角色 knowledge_scope_tags）
    title: str = ""  # 标题（可选）


@dataclass
class PreChunk:
    """切块后、embedding 前的中间产物。

    id 全局唯一，格式 ``{doc_id}_{idx}``，与最终入库的 Chunk.id 一致。
    source 继承自所属文档（检索时按角色 knowledge_scope_tags 过滤）。
    """

    id: str
    doc_id: str
    text: str
    source: str
    title: str


def split_document(doc: Document, max_chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """把文档正文切成 chunk 文本列表。

    策略（优先级从高到低）：
    1. 按段落切（连续两个换行 ``\\n\\n`` 分隔），保留段落完整性。
    2. 段落超过 ``max_chunk_size`` → 按句子（``. `` / ``。`` 边界）聚合，每块不超过上限。
    3. 单句仍超上限 → 硬切，按 ``max_chunk_size`` 滑窗，带 ``overlap`` 重叠保上下文。

    返回纯文本列表（不含元数据，元数据由 ``documents_to_pre_chunks`` 统一打上）。
    空文档返回空列表。
    """
    text = doc.text.strip()
    if not text:
        return []

    chunks: list[str] = []
    # 1. 段落级切分：连续空行视为段落边界。
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for para in paragraphs:
        if len(para) <= max_chunk_size:
            chunks.append(para)
        else:
            # 2/3. 超长段落 → 句子聚合 或 硬切。
            chunks.extend(_split_long_block(para, max_chunk_size, overlap))
    return chunks


def _split_long_block(block: str, max_size: int, overlap: int) -> list[str]:
    """超长段落二次切：先按句子聚合，单句仍超则硬切带 overlap。"""
    sentences = _split_sentences(block)
    out: list[str] = []
    buf = ""

    for sent in sentences:
        # 单句本身就超长 → 先把 buf 冲出去，再硬切这句。
        if len(sent) > max_size:
            if buf:
                out.append(buf.strip())
                buf = ""
            out.extend(_hard_split(sent, max_size, overlap))
            continue
        # 聚合：能塞就塞，塞不下就开新块。
        if buf and len(buf) + 1 + len(sent) > max_size:
            out.append(buf.strip())
            # overlap：保留上一块尾部，衔接上下文。
            buf = _tail(buf, overlap) + " " + sent if overlap else sent
        else:
            buf = f"{buf} {sent}".strip() if buf else sent
    if buf.strip():
        out.append(buf.strip())
    return out


def _split_sentences(text: str) -> list[str]:
    """简单句子切分：按中英文句号/问号/叹号 + 换行。

    不追求完美（M1.5 不引 NLP 库），保留切出来的分隔符在句尾。
    """
    out: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if ch in "。.!?！？\n":
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return [s for s in (x.strip() for x in out) if s]


def _hard_split(text: str, max_size: int, overlap: int) -> list[str]:
    """硬滑窗切分：按 max_size 步进，带 overlap 重叠。"""
    out: list[str] = []
    step = max(1, max_size - overlap)
    i = 0
    while i < len(text):
        out.append(text[i : i + max_size])
        if i + max_size >= len(text):
            break
        i += step
    return out


def _tail(s: str, n: int) -> str:
    """取字符串末尾 n 个字符（overlap 用）。"""
    return s[-n:] if n > 0 else ""


def documents_to_pre_chunks(docs: list[Document]) -> list[PreChunk]:
    """多个文档 → PreChunk 列表。

    每个文档用默认参数切块（max_chunk_size=500, overlap=50），
    chunk id 格式 ``{doc_id}_{idx}``（idx 在该文档内从 0 起递增）。
    """
    pre: list[PreChunk] = []
    for doc in docs:
        texts = split_document(doc)
        for idx, t in enumerate(texts):
            pre.append(
                PreChunk(
                    id=f"{doc.doc_id}_{idx}",
                    doc_id=doc.doc_id,
                    text=t,
                    source=doc.source,
                    title=doc.title,
                )
            )
    return pre
