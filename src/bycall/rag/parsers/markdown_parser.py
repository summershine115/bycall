"""Markdown 结构化解析器——提取 heading 结构为 Section。

与纯文本解析器的区别：利用 Markdown 的 `#`/`##`/`###` 层级信息
构建树形 Section 结构，供 chunking 层做 heading-aware 切分。

参考 WeKnora 的 heading_splitter：每个 chunk 附带 heading breadcrumb 上下文。
"""

from __future__ import annotations

import re

from bycall.rag.parsers.base import DocumentParser, ParseResult, Section

# 匹配 Markdown heading 行
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


class MarkdownParser(DocumentParser):
    """Markdown 结构化解析器。

    提取 heading 层级 + 正文，输出树形 Section 结构。
    同时去 Markdown 标记产纯文本（供全文检索使用）。
    """

    @property
    def name(self) -> str:
        return "markdown"

    @property
    def description(self) -> str:
        return "Markdown 结构化解析器——提取 heading 层级 + 正文"

    @property
    def file_types(self) -> list[str]:
        return ["md", "markdown"]

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析 Markdown 文件。"""
        text = content.decode("utf-8", errors="replace")
        return self.parse_text(text, title=file_name.rsplit(".", 1)[0] if file_name else "")

    def parse_text(self, text: str, title: str = "") -> ParseResult:
        """解析 Markdown 文本。"""
        sections = _extract_sections(text)
        plain_text = _strip_markdown(text)

        return ParseResult(
            text=plain_text,
            title=title,
            sections=sections,
        )


# ============ 内部 ============


def _extract_sections(text: str) -> list[Section]:
    """从 Markdown 文本提取 Section 树。

    按 #/##/### 等 heading 行切分，同级 heading 合并到同一层级。
    """
    lines = text.split("\n")
    root_sections: list[Section] = []
    # stack: [(level, heading, content_lines)]
    stack: list[tuple[int, str, list[str]]] = []

    def _flush_to(level: int) -> None:
        """把 stack 中 >= level 的段弹出，追加到上级或 root。"""
        while stack and stack[-1][0] >= level:
            lvl, hd, clines = stack.pop()
            sec = Section(heading=hd, level=lvl, content="\n".join(clines).strip())
            if stack:
                stack[-1][2].append(f"## {hd}\n{sec.content}")  # 子段内容并入父段
                if sec.content:
                    stack[-1][2].append(sec.content)
            else:
                root_sections.append(sec)

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            # 弹出 >= 当前 level 的旧 heading
            _flush_to(level)
            stack.append((level, heading, []))
        elif stack:
            stack[-1][2].append(line)

    # 清空残留 stack
    _flush_to(0)

    # 折叠：没有标题的内容追加到 root（作为纯文本 Section）
    return root_sections


def _strip_markdown(text: str) -> str:
    """去掉 Markdown 标记，输出纯文本。

    用 Python markdown 库转 HTML 再 strip tags，或用简单正则。
    这里用简单正则——markdown 库的扩展语法可能引入不需要的 HTML。
    """
    # 去掉代码块
    text = re.sub(r"```[\s\S]*?```", "\n", text)
    # 去掉行内代码
    text = re.sub(r"`[^`]+`", "", text)
    # 图片
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    # 链接保留文字
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # 强调标记
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    # 删除线
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    # 去掉行首 # 标记
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 去掉 > 引用标记
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # 去掉 -/* 列表标记
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
