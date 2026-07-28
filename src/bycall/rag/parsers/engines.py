"""解析引擎——文档解析的可切换策略层（位于按扩展名路由的解析器之上）。

设计动机：``registry.py`` 是**按文件扩展名**路由的解析器（每类型一个实现），
而 MarkItDown / MinerU 是**通用**解析器——一个引擎覆盖多种类型。因此在解析器之上
引入「解析引擎」策略层，参考 ``vector_store`` 的可切换后端设计：

- ``BuiltinEngine``：委托给 ``registry`` 的按扩展名解析器（MuPDF/python-docx/...），零外部
  依赖，是默认引擎，保证现有行为。
- ``MarkItDownEngine``：Microsoft MarkItDown 通用解析器（pdf/docx/pptx/xlsx/html/...），
  需 ``pip install markitdown``，输出 Markdown（复用 ``MarkdownParser`` 抽 heading 结构）。
- ``MinerUEngine``：MinerU 远程 API（布局识别 + OCR），适合扫描件 / 复杂版式 PDF。

路由规则（``parse_document``）：
1. 解析扩展名 → 选引擎（显式 ``engine`` 参数 > 配置默认）。
2. 引擎**不可用**（依赖未装 / 远程未配置）且是被显式选中的 → 抛错（避免静默回退掩盖配置错误）；
   引擎不可用但本就是默认 builtin → 直接走 builtin。
3. 引擎**不支持该类型** → 退回 builtin（markitdown/mineru 不覆盖的代码、配置等小众类型）。
4. 引擎解析抛错 → 上抛（真实解析错误，由 API 层转 4xx）。
"""

from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bycall.rag.parsers.base import ParseResult
from bycall.rag.parsers.registry import get_parser, list_parsers

if TYPE_CHECKING:
    from bycall.config import AppConfig

logger = logging.getLogger(__name__)


class ParseEngine(ABC):
    """解析引擎接口：声明支持的类型 + 解析二进制为 ParseResult。

    与 ``DocumentParser`` 的区别：引擎是**通用**策略（一个引擎多类型），
    ``DocumentParser`` 是**按类型**的实现（每类型一个）。引擎内部可复用 DocumentParser。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎名（builtin / markitdown / mineru）。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """人类可读说明。"""

    @property
    @abstractmethod
    def supported_types(self) -> list[str]:
        """支持的文件扩展名（无点，小写）。``["*"]`` 表示通配。"""

    @property
    def reason_unavailable(self) -> str:
        """不可用原因（``is_available`` 为 False 时填，供报错/列表展示）。"""
        return ""

    @abstractmethod
    def is_available(self) -> bool:
        """后端是否就绪（依赖已装 / 远程已配置）。"""

    def supports(self, file_type: str) -> bool:
        """是否声明支持某扩展名。``["*"]`` 通配。"""
        types = self.supported_types
        return "*" in types or file_type.lower() in types

    @abstractmethod
    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        """解析二进制内容为 ParseResult。"""

    def parse_text(self, text: str, title: str = "") -> ParseResult:
        """纯文本解析——引擎默认不处理纯文本，统一走 TextParser（manual 创建路径）。"""
        from bycall.rag.parsers.text_parser import TextParser  # 避免循环导入

        return TextParser().parse_text(text, title=title)


# ============ BuiltinEngine：委托按扩展名路由的解析器注册表 ============


class BuiltinEngine(ParseEngine):
    """内置引擎——按文件扩展名路由到 ``registry`` 的本地解析器。

    等价于引入引擎层之前的原始行为。永远是 available 的兜底。
    """

    @property
    def name(self) -> str:
        return "builtin"

    @property
    def description(self) -> str:
        return "内置解析器（按扩展名路由：MuPDF/python-docx/bs4/Markdown/Text）"

    @property
    def supported_types(self) -> list[str]:
        # 汇总所有已注册解析器声明的扩展名。
        types: set[str] = set()
        for p in list_parsers():
            types.update(p["file_types"])  # type: ignore[arg-type]
        return sorted(types)

    def is_available(self) -> bool:
        return True

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        ext = _ext_of(file_name)
        parser = get_parser(ext)
        if parser is None:
            raise ValueError(f"未找到 .{ext} 的内置解析器")
        return parser.parse(content, file_name=file_name)


# ============ MarkItDownEngine：Microsoft MarkItDown 通用解析器 ============


# MarkItDown 内置 converter 覆盖的常见文档类型（保守枚举，避免对冷门格式误声明）。
_MARKITDOWN_TYPES = [
    "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls",
    "html", "htm", "csv", "json", "xml", "txt", "text",
    "md", "markdown", "rst",
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "tif",
    "mp3", "wav", "m4a",
    "zip",
]


class MarkItDownEngine(ParseEngine):
    """MarkItDown 引擎——Microsoft 开源通用解析器（本地库）。

    依赖 ``markitdown``（``pip install markitdown``）。未安装时 ``is_available`` 为 False，
    ``parse_document`` 会让其声明的类型退回 builtin。
    输出为 Markdown，复用 ``MarkdownParser`` 抽 heading 结构以利于切块。
    """

    def __init__(self, *, enable_plugins: bool = False) -> None:
        self._enable_plugins = enable_plugins

    @property
    def name(self) -> str:
        return "markitdown"

    @property
    def description(self) -> str:
        return "Microsoft MarkItDown 通用解析器（本地库，需 pip install markitdown）"

    @property
    def supported_types(self) -> list[str]:
        return list(_MARKITDOWN_TYPES)

    @property
    def reason_unavailable(self) -> str:
        return "未安装 markitdown 库（pip install markitdown）"

    def is_available(self) -> bool:
        try:
            import markitdown  # noqa: F401 — 仅探测可导入性
        except ImportError:
            return False
        return True

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        from markitdown import MarkItDown, StreamInfo

        md = MarkItDown(enable_plugins=self._enable_plugins)
        ext = _ext_of(file_name)
        stream_info = StreamInfo(extension=f".{ext}", filename=file_name)
        result = md.convert_stream(io.BytesIO(content), stream_info=stream_info)

        markdown_text = (result.markdown or "").strip()
        title = (result.title or "").strip()

        # 复用 Markdown 结构化解析：从 heading 抽 Section，供 chunking 层 heading-aware 切分。
        from bycall.rag.parsers.markdown_parser import MarkdownParser

        parsed = MarkdownParser().parse_text(markdown_text, title=title or file_name)
        # 保留引擎来源标记，便于排查解析质量。
        parsed.metadata = {**parsed.metadata, "engine": "markitdown"}
        return parsed


# ============ MinerUEngine：MinerU 远程 API（布局识别 + OCR） ============


# MinerU 支持的上传类型（核实自 fast_api SUPPORTED_UPLOAD_SUFFIXES）。
_MINERU_TYPES = [
    # pdf
    "pdf",
    # 图片
    "png", "jpg", "jpeg", "bmp", "tiff", "tif", "gif", "webp",
    # office（需服务端装 LibreOffice 才能转；声明支持，失败由解析报错暴露）
    "docx", "doc", "pptx", "ppt", "xlsx", "xls",
]


class MinerUEngine(ParseEngine):
    """MinerU 引擎——远程 API 解析（布局识别 + OCR，适合扫描件 / 复杂版式）。

    依赖配置 ``mineru_base_url``（自建 mineru-api 或 mineru.net 云端）。未配置时
    ``is_available`` 为 False。每次解析是一次远程异步任务（提交→轮询→取结果）。
    """

    def __init__(self, *, base_url: str, api_token: str = "", backend: str = "hybrid-auto-engine",
                 parse_method: str = "auto", lang: str = "ch", timeout: float = 600.0,
                 poll_interval: float = 2.0, client=None) -> None:
        from bycall.rag.parsers.mineru_client import MinerUClient, MinerUConfig

        self._base_url = base_url
        self._client_factory = client  # 测试注入 fake client
        self._config = MinerUConfig(
            base_url=base_url,
            api_token=api_token,
            backend=backend,
            parse_method=parse_method,
            lang=lang,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        self._client = MinerUClient(self._config, http_client=client)

    @property
    def name(self) -> str:
        return "mineru"

    @property
    def description(self) -> str:
        return "MinerU 远程 API（布局识别 + OCR，需配置 mineru_base_url）"

    @property
    def supported_types(self) -> list[str]:
        return list(_MINERU_TYPES)

    @property
    def reason_unavailable(self) -> str:
        return "未配置 BYCALL_PARSE_MINERU_BASE_URL"

    def is_available(self) -> bool:
        return bool(self._base_url)

    def parse(self, content: bytes, file_name: str = "") -> ParseResult:
        from bycall.rag.parsers.markdown_parser import MarkdownParser
        from bycall.rag.parsers.mineru_client import MinerUError

        try:
            markdown_text = self._client.parse_to_markdown(content, file_name or "document.pdf")
        except MinerUError:
            raise
        except Exception as exc:  # 网络层等意外错误 → 统一为 MinerUError
            raise MinerUError(f"MinerU 解析异常: {exc}") from exc

        markdown_text = (markdown_text or "").strip()
        title = file_name.rsplit(".", 1)[0] if file_name else ""
        parsed = MarkdownParser().parse_text(markdown_text, title=title)
        parsed.metadata = {**parsed.metadata, "engine": "mineru"}
        return parsed


# ============ 引擎注册表 + 统一入口 ============


def get_parse_engine(name: str, *, settings: AppConfig | None = None) -> ParseEngine:
    """按名取引擎实例。未知名 → builtin（容错，不抛错）。"""
    settings = settings or _get_settings()
    if name == "markitdown":
        return MarkItDownEngine(enable_plugins=settings.parse.markitdown_enable_plugins)
    if name == "mineru":
        p = settings.parse
        return MinerUEngine(
            base_url=p.mineru_base_url,
            api_token=p.mineru_api_token.get_secret_value(),
            backend=p.mineru_backend,
            parse_method=p.mineru_parse_method,
            lang=p.mineru_lang,
            timeout=p.mineru_timeout,
            poll_interval=p.mineru_poll_interval,
        )
    return BuiltinEngine()


def list_parse_engines(*, settings: AppConfig | None = None) -> list[dict[str, object]]:
    """列出所有引擎及其可用性（供 admin /parse-engines 端点展示）。"""
    settings = settings or _get_settings()
    default = settings.parse.engine
    result: list[dict[str, object]] = []
    for name in ("builtin", "markitdown", "mineru"):
        eng = get_parse_engine(name, settings=settings)
        result.append(
            {
                "name": eng.name,
                "description": eng.description,
                "supported_types": eng.supported_types,
                "is_available": eng.is_available(),
                "reason_unavailable": eng.reason_unavailable,
                "is_default": name == default,
            }
        )
    return result


def resolve_engine_name(
    file_type: str,
    *,
    engine: str | None = None,
    engine_map: dict[str, str] | None = None,
    settings: AppConfig | None = None,
) -> str:
    """解析某文件类型应使用的引擎名（只决定名字，不实例化、不检查可用性）。

    优先级：显式 ``engine`` > ``engine_map[file_type]`` > ``engine_map["default"]``
    > 全局 ``settings.parse.engine``。上传端点的廉价预检与 ``parse_document`` 共用此函数，
    保证"预检的引擎"与"实际解析的引擎"一致。
    """
    settings = settings or _get_settings()
    if engine:
        return engine
    if engine_map:
        hit = engine_map.get(file_type) or engine_map.get("default")
        if hit:
            return hit
    return settings.parse.engine


def parse_document(
    content: bytes,
    file_name: str = "",
    *,
    engine: str | None = None,
    engine_map: dict[str, str] | None = None,
    settings: AppConfig | None = None,
) -> ParseResult:
    """统一解析入口——按引擎 + 类型路由，带 builtin 兜底。

    Args:
        content: 文件二进制。
        file_name: 文件名（推断扩展名 + 标题）。
        engine: 显式引擎名（最高优先级）。
        engine_map: per-KB 文件类型→引擎映射（如 ``{"pdf": "mineru", "default": "builtin"}``）。
        settings: 配置（测试注入）；None → ``get_settings()``。

    引擎解析优先级：engine > engine_map[ext] > engine_map["default"] > 全局默认。
    路由细则（不可用 / 不支持类型 / 解析报错）见模块 docstring。
    """
    settings = settings or _get_settings()
    ext = _ext_of(file_name)
    requested = resolve_engine_name(
        ext, engine=engine, engine_map=engine_map, settings=settings
    )

    # builtin 直接走（永远可用）。
    if requested == "builtin":
        return BuiltinEngine().parse(content, file_name=file_name)

    eng = get_parse_engine(requested, settings=settings)
    # 显式选中非 builtin 引擎却不可用 → 报错（暴露配置问题，不静默回退）。
    if not eng.is_available():
        raise ValueError(
            f"解析引擎 {requested!r} 不可用：{eng.reason_unavailable or '未知原因'}"
        )
    # 引擎不支持该类型 → 退回 builtin（小众类型不强制走所选引擎）。
    if not eng.supports(ext):
        logger.debug("引擎 %s 不支持 .%s，退回 builtin", requested, ext)
        return BuiltinEngine().parse(content, file_name=file_name)
    return eng.parse(content, file_name=file_name)


def supported_upload_types(*, settings: AppConfig | None = None) -> set[str]:
    """当前可上传的扩展名集合：builtin 支持的 ∪ 默认引擎支持的。"""
    settings = settings or _get_settings()
    types: set[str] = set(BuiltinEngine().supported_types)
    eng = get_parse_engine(settings.parse.engine, settings=settings)
    if eng.is_available():
        types.update(eng.supported_types)
    return types


# ============ 内部 ============


def _ext_of(file_name: str) -> str:
    """从文件名取小写无点扩展名；无扩展名 → 'txt'。"""
    if not file_name or "." not in file_name:
        return "txt"
    return file_name.rsplit(".", 1)[-1].lower()


def _get_settings() -> AppConfig | SimpleNamespace:
    """取解析配置：DB（SystemSetting，控制台可改）> .env / 环境变量 > 代码默认。

    显式传入 ``settings=`` 的调用方（测试注入、已持有 AppConfig 的端点）不走此路径。
    DB 无对应 key 时逐字段回退到 ``AppConfig``（env/.env/默认），与 IM 设置
    （``settings_store`` 同一机制）一致：控制台保存即时生效，无需重启。
    """
    from pydantic import SecretStr

    from bycall.config import get_settings
    from bycall.governance.settings_store import (
        get_setting_bool,
        get_setting_float,
        get_setting_value,
    )

    cfg = get_settings()
    p = cfg.parse

    def _enum(key: str, default: str, allowed: tuple[str, ...]) -> str:
        val = get_setting_value(key, default=default, env_var=f"BYCALL_PARSE_{key.upper()}")
        return val if val in allowed else default  # 非法值（历史脏数据）回退默认，不抛错

    return SimpleNamespace(
        parse=SimpleNamespace(
            engine=_enum("parse.engine", p.engine, ("builtin", "markitdown", "mineru")),
            markitdown_enable_plugins=get_setting_bool(
                "parse.markitdown_enable_plugins",
                default=p.markitdown_enable_plugins,
                env_var="BYCALL_PARSE_MARKITDOWN_ENABLE_PLUGINS",
            ),
            mineru_base_url=get_setting_value(
                "parse.mineru_base_url",
                default=p.mineru_base_url,
                env_var="BYCALL_PARSE_MINERU_BASE_URL",
            ),
            mineru_api_token=SecretStr(
                get_setting_value(
                    "parse.mineru_api_token",
                    default=p.mineru_api_token.get_secret_value(),
                    env_var="BYCALL_PARSE_MINERU_API_TOKEN",
                )
            ),
            mineru_backend=get_setting_value(
                "parse.mineru_backend",
                default=p.mineru_backend,
                env_var="BYCALL_PARSE_MINERU_BACKEND",
            ),
            mineru_parse_method=_enum(
                "parse.mineru_parse_method", p.mineru_parse_method, ("auto", "txt", "ocr")
            ),
            mineru_lang=get_setting_value(
                "parse.mineru_lang", default=p.mineru_lang, env_var="BYCALL_PARSE_MINERU_LANG"
            ),
            mineru_timeout=get_setting_float(
                "parse.mineru_timeout",
                default=p.mineru_timeout,
                env_var="BYCALL_PARSE_MINERU_TIMEOUT",
            ),
            mineru_poll_interval=get_setting_float(
                "parse.mineru_poll_interval",
                default=p.mineru_poll_interval,
                env_var="BYCALL_PARSE_MINERU_POLL_INTERVAL",
            ),
        )
    )
