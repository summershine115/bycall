"""纯文本解析器——兜底解析器（所有 "text/*" 类型）。

为所有无法匹配其他解析器的格式提供最小解析能力。
同时作为 manual 文档创建的默认解析器。
"""

from __future__ import annotations

from bycall.rag.parsers.base import DocumentParser, ParseResult


class TextParser(DocumentParser):
    """纯文本兜底解析器。"""

    @property
    def name(self) -> str:
        return "text"

    @property
    def description(self) -> str:
        return "纯文本兜底解析器——处理 txt/json/csv/xml/yaml 等格式"

    @property
    def file_types(self) -> list[str]:
        # 覆盖常见文本格式
        return [
            "txt", "text", "csv", "json", "xml", "yaml", "yml",
            "py", "js", "ts", "java", "go", "rs", "c", "cpp", "h",
            "sh", "bash", "zsh",
            "sql", "r", "rb", "swift", "kt",
            "toml", "ini", "cfg", "conf", "log",
        ]

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析文本文件。"""
        text = content.decode("utf-8", errors="replace")
        title = file_name.rsplit(".", 1)[0] if file_name else ""
        return ParseResult(text=text, title=title)

    def parse_text(self, text: str, title: str = "") -> ParseResult:
        return ParseResult(text=text, title=title)
