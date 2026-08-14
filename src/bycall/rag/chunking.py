"""文档切块——自适应策略 + 父子块（参考 WeKnora chunker 包）。

自适应 3 级策略（profiler.py → strategy.go）：
1. **heading**：按 Markdown heading / 文档 Section 层级切分（heading breadcrumb context）
2. **heuristic**：按分页符 / 编号章节 / 分隔线切分
3. **recursive**：递归优先分隔符切分（\\n\\n → \\n → 句号 → 逗号）

父子块（strategy.go SplitParentChild）：
- parent 块：大粒度上下文（4096 字符），仅做上下文不做向量索引
- child 块：小粒度精确匹配（384 字符），参与向量索引和检索
- 每个 child 指向 parent，召回时带回父块完整上下文

依赖最小：无 langchain 依赖，纯 Python 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Document:
    """待入库的原始文档。"""

    doc_id: str  # 文档唯一 id
    text: str  # 正文
    source: str  # 知识域标签
    title: str = ""  # 标题
    level: int = 0  # 密级/职级（ABAC），默认 0 = 公开


@dataclass
class Section:
    """文档的结构化段落——供 heading-aware 切分使用。"""

    heading: str = ""
    level: int = 1  # 1=h1, 2=h2, ...
    content: str = ""


@dataclass
class PreChunk:
    """切块后、embedding 前的中间产物。

    父子块：
    - chunk_type="child" + parent_id 指向父块
    - 父块 chunk_type="parent"，content 完整但仅做上下文
    """

    id: str
    doc_id: str
    text: str
    source: str
    title: str
    level: int = 0  # 密级/职级（透传到 chunk ACL）
    chunk_index: int = 0
    chunk_type: str = "child"  # "child" | "parent"
    parent_id: str | None = None  # 子块→父块 id


# ============ 切分配置 ============


@dataclass
class ChunkingConfig:
    """切分配置——按 KB 可调。

    默认值参考 WeKnora：
    - child: 384 字符 (~95 tokens) — 精确匹配粒度
    - parent: 4096 字符 (~1000 tokens) — 完整段落上下文
    """

    chunk_size: int = 500  # 默认单块字符数（非父子模式）
    overlap: int = 50  # 重叠字符数
    parent_size: int = 4096  # 父块字符数（父子模式）
    child_size: int = 384  # 子块字符数（父子模式）
    enable_parent_child: bool = False  # 是否开启父子块切分
    strategy: str = "auto"  # "auto" | "heading" | "recursive"
    separators: list[str] = field(
        default_factory=lambda: [
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            "；",
            ".",
            "!",
            "?",
            ";",
            "，",
            ",",
            " ",
        ]
    )


# ============ 主入口 ============


def split_document(
    doc: Document,
    config: ChunkingConfig | None = None,
    sections: list[Section] | None = None,
) -> list[str]:
    """把文档正文切成 chunk 文本列表（向后兼容接口）。"""
    cfg = config or ChunkingConfig()
    if not doc.text.strip():
        return []
    chunks = _split_adaptive(doc.text, cfg)
    return chunks


def split_parent_child(
    text: str,
    config: ChunkingConfig | None = None,
) -> tuple[list[str], list[str]]:
    """父子块切分：返回 (parent_texts, child_texts)。

    parent[i] 对应 child[i]——子块是父块的子片段，
    父块数量 ≤ 子块数量（多个子块可能共用一个父块）。
    """
    cfg = config or ChunkingConfig(parent_size=4096, child_size=384)

    # 1. 先生成大父块
    parent_cfg = ChunkingConfig(
        chunk_size=cfg.parent_size,
        overlap=cfg.overlap,
        separators=cfg.separators,
    )
    parent_texts = _split_adaptive(text, parent_cfg)

    # 2. 每个父块内部切成子块
    child_texts: list[str] = []
    child_cfg = ChunkingConfig(
        chunk_size=cfg.child_size,
        overlap=max(1, cfg.child_size // 5),  # ~20% overlap
        separators=cfg.separators,
    )
    for parent_text in parent_texts:
        sub_chunks = _split_adaptive(parent_text, child_cfg)
        child_texts.extend(sub_chunks)

    return parent_texts, child_texts


def documents_to_pre_chunks(
    docs: list[Document],
    config: ChunkingConfig | None = None,
    sections: list[Section] | None = None,
) -> list[PreChunk]:
    """多个文档 → PreChunk 列表（带父子块支持）。"""
    cfg = config or ChunkingConfig()
    pre: list[PreChunk] = []

    for doc in docs:
        if not doc.text.strip():
            continue

        if cfg.enable_parent_child:
            parent_texts, _child_texts = split_parent_child(doc.text, cfg)
            parent_chunks: list[PreChunk] = []
            child_chunks: list[PreChunk] = []
            idx = 0

            # 创建父块 PreChunk
            for pi, pt in enumerate(parent_texts):
                pid = f"{doc.doc_id}_parent_{pi}"
                pc = PreChunk(
                    id=pid,
                    doc_id=doc.doc_id,
                    text=pt,
                    source=doc.source,
                    title=doc.title,
                    level=doc.level,
                    chunk_index=idx,
                    chunk_type="parent",
                )
                parent_chunks.append(pc)
                idx += 1

            # 创建子块 PreChunk，每个指向其父块
            # 子块与父块的对应关系：sub_chunks 按父块顺序排列
            for pi, pt in enumerate(parent_texts):
                sub_chunks = _split_adaptive(
                    pt,
                    ChunkingConfig(
                        chunk_size=cfg.child_size,
                        overlap=max(1, cfg.child_size // 5),
                        separators=cfg.separators,
                    ),
                )
                for ci, ct in enumerate(sub_chunks):
                    cid = f"{doc.doc_id}_child_{pi}_{ci}"
                    cc = PreChunk(
                        id=cid,
                        doc_id=doc.doc_id,
                        text=ct,
                        source=doc.source,
                        title=doc.title,
                        level=doc.level,
                        chunk_index=idx,
                        chunk_type="child",
                        parent_id=parent_chunks[pi].id,
                    )
                    child_chunks.append(cc)
                    idx += 1

            # 父块只存 fields 不参与向量检索：记录到 pre 但标记为 parent
            pre.extend(parent_chunks)
            pre.extend(child_chunks)
        else:
            chunk_texts = _split_adaptive(doc.text, cfg)
            for i, ct in enumerate(chunk_texts):
                pre.append(
                    PreChunk(
                        id=f"{doc.doc_id}_{i}",
                        doc_id=doc.doc_id,
                        text=ct,
                        source=doc.source,
                        title=doc.title,
                        level=doc.level,
                        chunk_index=i,
                        chunk_type="child",
                    )
                )

    return pre


# ============ 自适应切分 ============


def _split_adaptive(text: str, config: ChunkingConfig) -> list[str]:
    """自适应切分：检测文档特征 → 选策略 → 切分。

    策略选择逻辑（参考 WeKnora profiler.go SelectStrategy）：
    - 标题密度 > 0.005 → heading 策略
    - 否则 → recursive 策略
    """
    strategy = config.strategy
    if strategy == "auto":
        strategy = _detect_strategy(text)
    raw = (
        _split_by_heading(text, config) if strategy == "heading" else _split_recursive(text, config)
    )
    # 过滤空/纯空白块：连续分隔符或文档首尾的空白区会被 split 切出空白块
    # （见 _split_by_separator 的 text.split(sep) 产物不过滤）。这种块直接送 embedding
    # 会被部分 provider 拒绝——硅基流动 bge 对空/空白输入返回 code 20015「参数无效」，
    # 整批 400 导致该批 chunk 全部丢失。空白块本就无语义价值，统一丢弃。
    return [c for c in raw if c and c.strip()]


def _detect_strategy(text: str) -> str:
    """检测文档特征，返回最佳策略名。

    简单启发式：Markdown heading 行数 ≥ 3 且密度 > 0.005 → heading。
    """
    lines = text.split("\n")
    heading_lines = sum(1 for line in lines if line.lstrip().startswith("#"))
    density = heading_lines / max(len(lines), 1)
    if heading_lines >= 3 and density > 0.005:
        return "heading"
    return "recursive"


def _split_by_heading(text: str, config: ChunkingConfig) -> list[str]:
    """按 Markdown heading 切分，每个 heading 组作为一个 chunk。

    参考 WeKnora heading_splitter.go：使用 heading breadcrumb context。
    如果 heading 组超过 chunk_size，递归切分该组的正文。
    """
    import re

    heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    chunks: list[str] = []
    current_lines: list[str] = []
    current_breadcrumb: list[str] = []  # heading 面包屑
    current_size = 0

    lines = text.split("\n")
    for line in lines:
        m = heading_re.match(line)
        if m:
            # 新的 heading → 之前累积的内容作为一个 chunk
            if current_lines:
                chunk_text = "\n".join(current_lines)
                if current_size > config.chunk_size:
                    chunks.extend(_split_recursive(chunk_text, config))
                else:
                    chunks.append(chunk_text)

            level = len(m.group(1))
            heading = m.group(2).strip()
            # 更新 breadcrumb
            while current_breadcrumb and len(current_breadcrumb) >= level:
                current_breadcrumb.pop()
            current_breadcrumb.append(heading)
            # 重建 heading 上下文行
            breadcrumb_text = " > ".join(current_breadcrumb)
            current_lines = [f"# {breadcrumb_text}", ""]
            current_size = 0
        else:
            current_lines.append(line)
            current_size += len(line)

    # 最后一段
    if current_lines:
        chunk_text = "\n".join(current_lines)
        if current_size > config.chunk_size:
            chunks.extend(_split_recursive(chunk_text, config))
        else:
            chunks.append(chunk_text)

    return chunks if chunks else [text]


def _split_recursive(text: str, config: ChunkingConfig) -> list[str]:
    """递归优先分隔符切分（参考 WeKnora recursive splitter）。

    按 separators 优先级依次尝试切分：
    \\n\\n → \\n → 。 → ！ → ？ → ； → . → ! → ? → ; → ， → , → 空格 → 字符级硬切。
    """
    if len(text) <= config.chunk_size:
        return [text] if text.strip() else []

    return _split_by_separator(text, config, config.separators)


def _split_by_separator(text: str, config: ChunkingConfig, separators: list[str]) -> list[str]:
    """按给定分隔符列表，递归切分直到每个片段 ≤ chunk_size。"""
    if not separators or len(text) <= config.chunk_size:
        return _hard_split_final(text, config)

    sep = separators[0]
    remaining = separators[1:]

    parts = text.split(sep)
    if len(parts) == 1:
        # 当前分隔符没切出多段 → 试下一个
        return _split_by_separator(text, config, remaining)

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for part in parts:
        part_with_sep = part + (sep if sep in ("\n\n", "\n") else "")
        part_size = len(part_with_sep)

        if current_size + part_size > config.chunk_size and current:
            # 当前块已满 → 输出
            chunk_text = "".join(current) if sep in ("\n\n", "\n") else sep.join(current)
            if len(chunk_text) > config.chunk_size:
                chunks.extend(_split_by_separator(chunk_text, config, remaining))
            else:
                chunks.append(chunk_text)
            current = []
            current_size = 0

        current.append(part)
        current_size += part_size

    # 最后一块
    if current:
        chunk_text = "".join(current) if sep in ("\n\n", "\n") else sep.join(current)
        if len(chunk_text) > config.chunk_size:
            chunks.extend(_split_by_separator(chunk_text, config, remaining))
        else:
            chunks.append(chunk_text)

    return chunks


def _hard_split_final(text: str, config: ChunkingConfig) -> list[str]:
    """字符级硬切：无合适分隔符时的最终兜底。"""
    if not text.strip():
        return []
    if len(text) <= config.chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + config.chunk_size, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break  # 已到末尾：必须终止，否则 start=end-overlap 会让末尾重叠段无限循环
        start = end - config.overlap
    return chunks


# ============ 保持向后兼容的函数 ============


def _split_long_block(block: str, max_size: int, overlap: int) -> list[str]:
    """向后兼容：把单段文本按句子切分（旧接口）。"""
    cfg = ChunkingConfig(chunk_size=max_size, overlap=overlap)
    return _split_recursive(block, cfg)


def _split_sentences(text: str) -> list[str]:
    """简单句子切分（向后兼容）。"""
    import re

    parts = re.split(r"([。！？.!?\n])", text)
    out: list[str] = []
    buf = ""
    for part in parts:
        if part in ("。", "！", "？", ".", "!", "?", "\n"):
            buf += part
            out.append(buf)
            buf = ""
        else:
            buf += part
    if buf.strip():
        out.append(buf)
    return [s.strip() for s in out if s.strip()]


def _hard_split(text: str, max_size: int, overlap: int) -> list[str]:
    """硬滑窗切分（向后兼容）。"""
    cfg = ChunkingConfig(chunk_size=max_size, overlap=overlap)
    return _hard_split_final(text, cfg)


def _tail(s: str, n: int) -> str:
    """取字符串末尾 n 个字符（overlap 用）。"""
    return s[-n:] if n > 0 else ""
