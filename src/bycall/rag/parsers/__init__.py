"""文档解析器——插件化架构（参考 WeKnora Engine Registry）。

每个解析器实现 ``DocumentParser`` 接口，通过 ``register_parser`` / ``get_parser``
按文件类型路由。新增格式只需实现一个类并注册，无需改调用方。

支持的格式：
- PDF：MuPDF（pymupdf），提取文字 + 章节结构 + 表格
- Word (.docx)：python-docx，带段落样式信息
- Markdown：heading 结构 + 正文，保留标题层级
- HTML：beautifulsoup4，去标签 + 保留结构
- Text：纯文本兜底
"""

from bycall.rag.parsers.base import DocumentParser, ParseResult
from bycall.rag.parsers.engines import (
    ParseEngine,
    get_parse_engine,
    list_parse_engines,
    parse_document,
    resolve_engine_name,
    supported_upload_types,
)
from bycall.rag.parsers.registry import get_parser, list_parsers, register_parser

__all__ = [
    "DocumentParser",
    "ParseEngine",
    "ParseResult",
    "get_parse_engine",
    "get_parser",
    "list_parse_engines",
    "list_parsers",
    "parse_document",
    "register_parser",
    "resolve_engine_name",
    "supported_upload_types",
]
