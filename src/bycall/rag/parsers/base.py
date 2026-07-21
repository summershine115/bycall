"""文档解析器接口——参考 WeKnora EngineRegistration。

每个解析器声明自己能处理的文件类型，运行时按文件扩展名路由。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ParseResult:
    """解析器输出——结构化的纯文本 + 元数据。

    ``sections`` 保留文档结构（标题层级 + 文本），供 chunking 层按结构切分。
    ``metadata`` 保留解析器提取的非正文信息（作者/创建时间/页数等），
    可用于检索时的 source 过滤或展示。
    """

    text: str  # 全文纯文本（去格式后）
    title: str = ""  # 文档标题（从文件名或元数据推断）
    sections: list[Section] = field(default_factory=list)  # 结构化段落（标题+正文）
    metadata: dict[str, object] = field(default_factory=dict)  # 元数据


@dataclass
class Section:
    """文档的结构化段落——带标题层级信息。"""

    heading: str  # 标题文本
    level: int  # 层级（1=h1, 2=h2, ...）
    content: str  # 该标题下的正文
    children: list[Section] = field(default_factory=list)  # 子段落


class DocumentParser(ABC):
    """文档解析器接口。

    实现类需声明 ``name``、``description``、``file_types``，
    并提供 ``parse`` 方法。按文件扩展名路由。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """解析器名称（如 "pdf", "docx", "markdown"）。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """解析器描述（如 "MuPDF PDF 解析器"）。"""

    @property
    @abstractmethod
    def file_types(self) -> list[str]:
        """支持的文件扩展名列表（如 ["pdf"], ["docx", "doc"]）。"""

    @abstractmethod
    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析文件二进制内容。

        Args:
            content: 文件二进制内容。
            file_name: 文件名（用于推断标题和格式）。

        Returns:
            ParseResult: 结构化文本 + 元数据。
        """

    @abstractmethod
    def parse_text(self, text: str, title: str = "") -> ParseResult:
        """解析纯文本（用于 manual 创建文档的解析路径）。

        Args:
            text: 文本内容。
            title: 文档标题。

        Returns:
            ParseResult: 结构化文本。
        """
