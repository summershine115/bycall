"""HTML 解析器——基于 beautifulsoup4 提取文字 + 结构。

去标签保留正文，按 h1-h6 标签构建 Section 树。
脚本和样式内容自动去除。
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from bycall.rag.parsers.base import DocumentParser, ParseResult, Section

logger = logging.getLogger(__name__)


class HtmlParser(DocumentParser):
    """BeautifulSoup HTML 解析器。

    去标签保留正文，按 h1-h6 标签构建 Section 树。
    """

    @property
    def name(self) -> str:
        return "html"

    @property
    def description(self) -> str:
        return "BeautifulSoup HTML 解析器——去标签保留正文 + 结构"

    @property
    def file_types(self) -> list[str]:
        return ["html", "htm", "mhtml", "mht"]

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析 HTML 内容。"""
        title = file_name.rsplit(".", 1)[0] if file_name else ""
        text = content.decode("utf-8", errors="replace")
        return self.parse_text(text, title=title)

    def parse_text(self, text: str, title: str = "") -> ParseResult:
        """解析 HTML 文本。"""
        soup = BeautifulSoup(text, "lxml")

        # 标题
        page_title = title
        if soup.title and soup.title.string:
            page_title = soup.title.string.strip()

        # 去脚本和样式
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        sections: list[Section] = []
        full_text_parts: list[str] = []

        current_heading = ""
        current_level = 0
        current_content: list[str] = []

        def _flush_section() -> None:
            nonlocal current_heading, current_level, current_content
            if current_heading or current_content:
                sections.append(Section(
                    heading=current_heading or "",
                    level=max(current_level, 1),
                    content="\n".join(current_content),
                ))
            current_heading = ""
            current_level = 0
            current_content = []

        body = soup.body or soup
        for tag in body.descendants:
            if tag.name in (f"h{i}" for i in range(1, 7)):
                level = int(tag.name[1])
                tag_text = tag.get_text(" ", strip=True)
                if not tag_text:
                    continue
                _flush_section()
                current_heading = tag_text
                current_level = level
            elif tag.name in ("p", "li", "td", "th", "div", "pre", "blockquote", "article", "section"):
                tag_text = tag.get_text(" ", strip=True)
                if tag_text:
                    full_text_parts.append(tag_text)
                    current_content.append(tag_text)
            elif tag.string and tag.name is None:
                # 裸文本节点（body 直子文本）
                t = tag.string.strip()
                if t:
                    full_text_parts.append(t)

        _flush_section()

        full_text = "\n\n".join(full_text_parts)

        return ParseResult(
            text=full_text,
            title=page_title,
            sections=sections,
            metadata={},
        )
