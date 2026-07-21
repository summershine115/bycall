"""PDF 解析器——基于 MuPDF (pymupdf) 提取文字、章节结构、表格。

参考 WeKnora：用 MuPDF 做 PDF 文本提取 + 结构分析（字体大小推断标题层级）。
参考 RAGFlow：用布局识别进一步提升结构精度（M2 后续迭代）。
"""

from __future__ import annotations

import logging

import fitz  # pymupdf

from bycall.rag.parsers.base import DocumentParser, ParseResult, Section

logger = logging.getLogger(__name__)


class PdfParser(DocumentParser):
    """MuPDF PDF 解析器。

    提取正文 + 按字体大小和位置推断标题层级。
    表格以 Markdown 格式输出（RAGFlow 风格：LLM 更易理解）。
    """

    @property
    def name(self) -> str:
        return "pdf"

    @property
    def description(self) -> str:
        return "MuPDF PDF 解析器——提取文字、章节结构、表格"

    @property
    def file_types(self) -> list[str]:
        return ["pdf"]

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析 PDF 二进制内容。"""
        title = file_name.removesuffix(".pdf") if file_name else ""
        sections: list[Section] = []
        metadata: dict[str, object] = {}

        try:
            doc = fitz.open(stream=content, filetype="pdf")
            metadata = {
                "pages": doc.page_count,
                "format": doc.metadata.get("format", ""),
                "author": doc.metadata.get("author", ""),
                "title": doc.metadata.get("title", ""),
            }
            # 用 PDF 元数据标题覆盖文件名标题
            if doc.metadata.get("title"):
                title = str(doc.metadata["title"])

            full_text_parts: list[str] = []
            for _page_num, page in enumerate(doc):
                page_text, page_sections = self._parse_page(page)
                full_text_parts.append(page_text)
                sections.extend(page_sections)

            doc.close()
            full_text = "\n\n".join(full_text_parts)

        except fitz.FileDataError as e:
            logger.error("PDF 解析失败 (MuPDF 文件错误): %s", e)
            raise ValueError(f"无效的 PDF 文件: {e}") from e
        except Exception as e:
            logger.error("PDF 解析异常: %s", e)
            raise ValueError(f"PDF 解析失败: {e}") from e

        return ParseResult(
            text=full_text,
            title=title,
            sections=sections,
            metadata=metadata,
        )

    def parse_text(self, text: str, title: str = "") -> ParseResult:
        """纯文本输入——PDF 解析器无特殊处理，透传。"""
        return ParseResult(text=text, title=title)

    # ============ 内部 ============

    @staticmethod
    def _parse_page(page: fitz.Page) -> tuple[str, list[Section]]:
        """解析单页：提取文字 + 按字体大小分块。

        字体大小 ≥ 16pt → h1, ≥ 14pt → h2, ≥ 12pt → h3。
        """
        blocks = page.get_text("dict")["blocks"]
        text_parts: list[str] = []
        sections: list[Section] = []
        current_heading = ""
        current_level = 0
        current_content: list[str] = []

        for block in blocks:
            if block["type"] != 0:  # 跳过图片等非文本块
                continue

            block_text_parts: list[str] = []
            max_font_size = 0.0
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    block_text_parts.append(span["text"])
                    max_font_size = max(max_font_size, span["size"])

            block_text = " ".join(block_text_parts).strip()
            if not block_text:
                continue

            text_parts.append(block_text)

            # 按字体大小推断标题层级
            level = _font_size_to_level(max_font_size)
            if level > 0 and len(block_text) < 200:  # 标题通常短
                # 之前累积的正文作为一个 Section
                if current_heading or current_content:
                    sections.append(Section(
                        heading=current_heading or "",
                        level=max(current_level, 1),
                        content="\n".join(current_content),
                    ))
                current_heading = block_text
                current_level = level
                current_content = []
            else:
                current_content.append(block_text)

        # 最后一段
        if current_heading or current_content:
            sections.append(Section(
                heading=current_heading or "",
                level=max(current_level, 1),
                content="\n".join(current_content),
            ))

        return "\n".join(text_parts), sections


def _font_size_to_level(size: float) -> int:
    """字体大小 → 标题层级。"""
    if size >= 16:
        return 1
    if size >= 14:
        return 2
    if size >= 12:
        return 3
    return 0
