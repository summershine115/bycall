"""解析器注册表——按文件扩展名路由（参考 WeKnora EngineRegistry）。

全局注册表，自动发现并注册内置解析器。
外部只需 ``get_parser("pdf")`` 获取 PDF 解析器实例。
"""

from __future__ import annotations

import logging

from bycall.rag.parsers.base import DocumentParser

logger = logging.getLogger(__name__)

_registry: dict[str, type[DocumentParser]] = {}


def register_parser(parser_cls: type[DocumentParser]) -> None:
    """注册解析器：按 parser_cls.file_types 的每个扩展名映射到该类。

    重复注册同一扩展名 → 后注册者覆盖先注册者（警告日志）。
    """
    # 实例化一次取属性（不存实例，每次 get_parser 新建，避免状态残留）
    probe = parser_cls()
    for ext in probe.file_types:
        if ext in _registry:
            logger.warning(
                "解析器 %s 覆盖了 %s 的扩展名 %s",
                parser_cls.__name__,
                _registry[ext].__name__,
                ext,
            )
        _registry[ext] = parser_cls
    logger.debug("注册解析器 %s: %s", parser_cls.__name__, probe.file_types)


def get_parser(file_type: str) -> DocumentParser | None:
    """按文件类型返回解析器实例。"""
    cls = _registry.get(file_type.lower())
    if cls is None:
        return None
    return cls()


def list_parsers() -> list[dict[str, object]]:
    """列出所有已注册的解析器。"""
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for _ext, cls in _registry.items():
        name = cls.__name__
        if name in seen:
            continue
        seen.add(name)
        probe = cls()
        result.append({
            "name": probe.name,
            "description": probe.description,
            "file_types": probe.file_types,
        })
    return result


# ============ 自动注册内置解析器 ============


def _register_builtins() -> None:
    """自动导入并注册所有内置解析器。"""
    from bycall.rag.parsers.docx_parser import DocxParser
    from bycall.rag.parsers.html_parser import HtmlParser
    from bycall.rag.parsers.markdown_parser import MarkdownParser
    from bycall.rag.parsers.pdf_parser import PdfParser
    from bycall.rag.parsers.text_parser import TextParser

    for cls in (PdfParser, DocxParser, MarkdownParser, HtmlParser, TextParser):
        register_parser(cls)


_register_builtins()
