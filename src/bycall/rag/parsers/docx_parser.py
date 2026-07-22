"""Word (.docx) 解析器——基于 python-docx 提取文字 + 段落样式。

参考 WeKnora / RAGFlow：按 Heading 样式推断标题层级（Heading 1 → h1）。
表格以 Markdown 格式输出。
"""

from __future__ import annotations

import logging
from io import BytesIO

from docx import Document as DocxDocument

from bycall.rag.parsers.base import DocumentParser, ParseResult, Section

logger = logging.getLogger(__name__)

# python-docx 内置的 heading 样式名映射
_HEADING_STYLE_MAP: dict[str, int] = {
    "Heading 1": 1,
    "heading 1": 1,
    "Heading 2": 2,
    "heading 2": 2,
    "Heading 3": 3,
    "heading 3": 3,
    "Heading 4": 4,
    "heading 4": 4,
    "Heading 5": 5,
    "heading 5": 5,
    "Heading 6": 6,
    "heading 6": 6,
    "Title": 1,
}


class DocxParser(DocumentParser):
    """python-docx Word 解析器。

    提取正文 + 按段落样式识别标题层级。
    表格 → Markdown 表格格式。
    """

    @property
    def name(self) -> str:
        return "docx"

    @property
    def description(self) -> str:
        return "python-docx Word 解析器——提取文字、段落样式、表格"

    @property
    def file_types(self) -> list[str]:
        return ["docx", "doc"]

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析 .docx 二进制内容。"""
        title = file_name.rsplit(".", 1)[0] if file_name else ""
        sections: list[Section] = []
        metadata: dict[str, object] = {}

        try:
            doc = DocxDocument(BytesIO(content))

            # 元数据
            props = doc.core_properties
            metadata = {
                "author": props.author or "",
                "title": props.title or "",
                "created": str(props.created) if props.created else "",
            }
            if props.title:
                title = str(props.title)

            text_parts: list[str] = []
            current_heading = ""
            current_level = 0
            current_content: list[str] = []

            for para in doc.paragraphs:
                para_text = para.text.strip()
                if not para_text:
                    continue

                text_parts.append(para_text)

                level = _get_heading_level((para.style.name or "") if para.style else "")

                if level > 0:
                    if current_heading or current_content:
                        sections.append(
                            Section(
                                heading=current_heading or "",
                                level=max(current_level, 1),
                                content="\n".join(current_content),
                            )
                        )
                    current_heading = para_text
                    current_level = level
                    current_content = []
                else:
                    current_content.append(para_text)

            # 最后一段
            if current_heading or current_content:
                sections.append(
                    Section(
                        heading=current_heading or "",
                        level=max(current_level, 1),
                        content="\n".join(current_content),
                    )
                )

            # 表格 → Markdown
            for table in doc.tables:
                md_table = _table_to_markdown(table)
                text_parts.append(md_table)

            full_text = "\n\n".join(text_parts)

        except Exception as e:
            logger.error("DOCX 解析异常: %s", e)
            raise ValueError(f"DOCX 解析失败: {e}") from e

        return ParseResult(
            text=full_text,
            title=title,
            sections=sections,
            metadata=metadata,
        )

    def parse_text(self, text: str, title: str = "") -> ParseResult:
        return ParseResult(text=text, title=title)


# ============ 内部 ============


def _get_heading_level(style_name: str) -> int:
    """段落样式名 → 标题层级。"""
    return _HEADING_STYLE_MAP.get(style_name, 0)


def _table_to_markdown(table) -> str:
    """python-docx Table → Markdown 表格字符串。"""
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        rows.append(cells)

    if not rows:
        return ""

    # 等宽格式化
    col_count = max(len(r) for r in rows)
    for r in rows:
        while len(r) < col_count:
            r.append("")

    lines: list[str] = []
    # header
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    # body
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
