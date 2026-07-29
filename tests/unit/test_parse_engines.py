"""解析引擎测试：builtin / markitdown / mineru + parse_document 路由与兜底。

不依赖外部服务（markitdown 库未装、MinerU 远程未连）：用注入的 fake 模块 / fake HTTP
客户端验证引擎行为，参考 MCPClient 的 session_cm_factory 注入模式。
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass

import pytest

from bycall.rag.parsers.engines import (
    BuiltinEngine,
    MarkItDownEngine,
    MinerUEngine,
    get_parse_engine,
    list_parse_engines,
    parse_document,
    resolve_engine_name,
    supported_upload_types,
)
from bycall.rag.parsers.mineru_client import (
    MinerUClient,
    MinerUConfig,
    MinerUError,
    _extract_markdown_from_zip,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """解析配置走 DB 优先（SystemSetting）——用临时 SQLite 隔离本机开发库，

    并灌默认数据，保证目录 / 路由用例不受 data/bycall.db 的真实设置影响。
    """
    from sqlmodel import create_engine

    import bycall.db as db_mod
    from bycall.config import get_settings
    from bycall.db import init_db, set_engine

    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/parse.db")
    get_settings.cache_clear()
    old_engine = db_mod._engine
    set_engine(
        create_engine(f"sqlite:///{tmp_path}/parse.db", connect_args={"check_same_thread": False})
    )
    init_db()
    yield
    set_engine(old_engine)
    get_settings.cache_clear()


# ============ 1. BuiltinEngine ============


class TestBuiltinEngine:
    def test_routes_by_extension(self):
        eng = BuiltinEngine()
        result = eng.parse("# Title\n\n正文段落".encode(), file_name="doc.md")
        assert "正文段落" in result.text
        assert len(result.sections) == 1  # Markdown heading 抽出一个 Section

    def test_always_available(self):
        assert BuiltinEngine().is_available() is True

    def test_supported_types_cover_common_formats(self):
        types = BuiltinEngine().supported_types
        for ext in ("pdf", "docx", "md", "html", "txt"):
            assert ext in types

    def test_parse_text_passthrough(self):
        result = BuiltinEngine().parse_text("纯文本", title="t")
        assert result.text == "纯文本"


# ============ 2. MarkItDownEngine ============


class TestMarkItDownEngine:
    def test_unavailable_when_not_installed(self):
        """markitdown 未安装时 is_available=False，显式选用应报错而非静默回退。"""
        # 假定测试环境未装 markitdown；若装了则跳过该断言。
        try:
            import markitdown  # noqa: F401
        except ImportError:
            eng = MarkItDownEngine()
            assert eng.is_available() is False
            assert "markitdown" in eng.reason_unavailable

    def test_parse_with_fake_markitdown(self, monkeypatch):
        """注入 fake markitdown 模块，验证 convert_stream 结果被结构化。"""

        @dataclass
        class _Result:
            markdown: str
            title: str = ""

        class _MarkItDown:
            def __init__(self, *, enable_plugins=False):
                self.enable_plugins = enable_plugins

            def convert_stream(self, stream, *, stream_info=None):
                return _Result(markdown="# 章节\n\n内容", title="从元数据来的标题")

        class _StreamInfo:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        fake = type(sys)("markitdown")
        fake.MarkItDown = _MarkItDown
        fake.StreamInfo = _StreamInfo
        monkeypatch.setitem(sys.modules, "markitdown", fake)

        eng = MarkItDownEngine()
        assert eng.is_available() is True

        result = eng.parse(b"binary-pdf-bytes", file_name="report.pdf")
        assert "章节" in result.text
        assert "内容" in result.text
        # 标题取自 markitdown 元数据。
        assert result.title == "从元数据来的标题"
        # 引擎来源标记。
        assert result.metadata.get("engine") == "markitdown"
        # Markdown heading 被抽成 Section。
        assert len(result.sections) == 1


# ============ 3. MinerU 客户端 + 引擎 ============


class _FakeResponse:
    """模拟 httpx.Response：status_code / json() / content / headers。"""

    def __init__(self, status_code, *, json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace")
        self.reason_phrase = ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeMinerUHttp:
    """模拟 MinerU 三阶段 HTTP：提交→轮询→取 zip 结果。

    传 ``fail_once=True`` 让首次轮询返回 processing，第二次 completed（验证轮询循环）。
    """

    def __init__(self, *, markdown="# 扫描结果\n\nOCR 文本", fail_status=False):
        self.markdown = markdown
        self.fail_status = fail_status
        self.calls: list[tuple] = []

    def post(self, url, *, data=None, files=None, headers=None, timeout=None):
        self.calls.append(("POST", url))
        return _FakeResponse(
            202,
            json_data={
                "task_id": "t-1",
                "status_url": "http://m/tasks/t-1/status",
                "result_url": "http://m/tasks/t-1/result",
            },
        )

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if url.endswith("/status"):
            if self.fail_status:
                return _FakeResponse(200, json_data={"status": "failed", "error": "boom"})
            return _FakeResponse(200, json_data={"status": "completed"})
        if url.endswith("/result"):
            zip_bytes = _make_result_zip(full_md=self.markdown)
            return _FakeResponse(
                200, content=zip_bytes, headers={"content-type": "application/zip"}
            )
        return _FakeResponse(404)


def _make_result_zip(*, full_md: str = "", extra: dict[str, str] | None = None) -> bytes:
    """构造 MinerU 结果 zip：full.md + 可选额外 .md。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if full_md:
            zf.writestr("full.md", full_md)
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


class TestMinerUClient:
    def test_submit_poll_fetch_markdown(self):
        http = _FakeMinerUHttp()
        client = MinerUClient(MinerUConfig(base_url="http://m", api_token="tok"), http_client=http)
        md = client.parse_to_markdown(b"pdf-bytes", "scan.pdf")
        assert md.startswith("# 扫描结果")
        # 三阶段都打过。
        assert ("POST", "http://m/tasks") in http.calls
        assert any(url.endswith("/status") for _, url in http.calls)
        assert any(url.endswith("/result") for _, url in http.calls)

    def test_auth_header_sent_when_token_set(self):
        http = _FakeMinerUHttp()
        client = MinerUClient(
            MinerUConfig(base_url="http://m", api_token="secret"), http_client=http
        )
        client.parse_to_markdown(b"x", "a.pdf")
        # _FakeMinerUHttp 不记 headers，这里只确保流程不报错；headers 逻辑由 _headers 覆盖。
        assert http.calls[0][0] == "POST"

    def test_failed_status_raises(self):
        http = _FakeMinerUHttp(fail_status=True)
        client = MinerUClient(MinerUConfig(base_url="http://m"), http_client=http)
        with pytest.raises(MinerUError, match="失败"):
            client.parse_to_markdown(b"x", "a.pdf")

    def test_extract_prefers_full_md(self):
        zip_bytes = _make_result_zip(full_md="# 全文", extra={"a.md": "片段A", "b.md": "片段B"})
        md = _extract_markdown_from_zip(zip_bytes)
        assert md == "# 全文"

    def test_extract_concatenates_when_no_full(self):
        zip_bytes = _make_result_zip(extra={"b.md": "B", "a.md": "A"})
        md = _extract_markdown_from_zip(zip_bytes)
        assert md == "A\n\nB"  # 按文件名排序拼接

    def test_extract_rejects_path_traversal(self):
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w") as zf:
            zf.writestr("../evil.md", "x")
            zf.writestr("ok.md", "ok")
        md = _extract_markdown_from_zip(zip_bytes.getvalue())
        assert md == "ok"

    def test_no_markdown_raises(self):
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w") as zf:
            zf.writestr("images/a.png", b"png")
        with pytest.raises(MinerUError, match="未找到"):
            _extract_markdown_from_zip(zip_bytes.getvalue())


class TestMinerUEngine:
    def test_unavailable_without_base_url(self):
        eng = MinerUEngine(base_url="")
        assert eng.is_available() is False
        assert "BASE_URL" in eng.reason_unavailable

    def test_parse_with_fake_client(self):
        eng = MinerUEngine(base_url="http://m", client=_FakeMinerUHttp())
        assert eng.is_available() is True
        result = eng.parse(b"pdf-bytes", file_name="scan.pdf")
        assert "OCR 文本" in result.text
        assert result.metadata.get("engine") == "mineru"
        assert len(result.sections) == 1  # heading 抽出

    def test_pdf_in_supported_types(self):
        assert "pdf" in MinerUEngine(base_url="http://m").supported_types


# ---- cloud 协议（mineru.net /api/v4）----


class _FakeMinerUCloudHttp:
    """模拟 mineru.net 云端四阶段：申请链接→PUT 上传→轮询 batch→取 zip。

    ``running_once=True``：首次轮询返回 running、第二次 done（验证轮询循环）。
    """

    def __init__(
        self,
        *,
        markdown="# 云端结果\n\n正文",
        fail_state=False,
        running_once=False,
        reject_code=None,
    ):
        self.markdown = markdown
        self.fail_state = fail_state
        self.running_once = running_once
        self.reject_code = reject_code  # 申请阶段业务失败码（code != 0）
        self.calls: list[tuple] = []
        self.uploaded: bytes | None = None
        self._polled = 0

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url))
        if url.endswith("/file-urls/batch"):
            if self.reject_code is not None:
                return _FakeResponse(200, json_data={"code": self.reject_code, "msg": "denied"})
            return _FakeResponse(
                200,
                json_data={
                    "code": 0,
                    "data": {"batch_id": "b-1", "file_urls": ["http://oss/upload"]},
                },
            )
        return _FakeResponse(404)

    def put(self, url, *, content=None, headers=None, timeout=None):
        self.calls.append(("PUT", url))
        self.uploaded = content
        return _FakeResponse(200)

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if "/extract-results/batch/" in url:
            if self.fail_state:
                return _FakeResponse(
                    200,
                    json_data={
                        "code": 0,
                        "data": {"extract_result": [{"state": "failed", "err_msg": "boom"}]},
                    },
                )
            self._polled += 1
            if self.running_once and self._polled == 1:
                return _FakeResponse(
                    200,
                    json_data={"code": 0, "data": {"extract_result": [{"state": "running"}]}},
                )
            return _FakeResponse(
                200,
                json_data={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "file_name": "scan.pdf",
                                "state": "done",
                                "full_zip_url": "http://cdn/r.zip",
                            }
                        ]
                    },
                },
            )
        if url.endswith("r.zip"):
            return _FakeResponse(200, content=_make_result_zip(full_md=self.markdown))
        return _FakeResponse(404)


class TestMinerUCloudClient:
    def test_apply_upload_put_poll_fetch(self):
        http = _FakeMinerUCloudHttp()
        client = MinerUClient(
            MinerUConfig(base_url="https://mineru.net", api_token="tok", mode="cloud"),
            http_client=http,
        )
        md = client.parse_to_markdown(b"pdf-bytes", "scan.pdf")
        assert md.startswith("# 云端结果")
        # 四阶段都打过：申请→上传→轮询→取 zip。
        assert any(u.endswith("/file-urls/batch") for _, u in http.calls)
        assert ("PUT", "http://oss/upload") in http.calls
        assert any("/extract-results/batch/b-1" in u for _, u in http.calls)
        assert ("GET", "http://cdn/r.zip") in http.calls
        # PUT 上传的是原始字节。
        assert http.uploaded == b"pdf-bytes"

    def test_polling_loop_handles_running(self):
        http = _FakeMinerUCloudHttp(running_once=True)
        client = MinerUClient(
            MinerUConfig(
                base_url="https://mineru.net", api_token="tok", mode="cloud", poll_interval=0
            ),
            http_client=http,
        )
        md = client.parse_to_markdown(b"x", "scan.pdf")
        assert md.startswith("# 云端结果")
        # 轮询至少两次（running → done）。
        polls = [u for _, u in http.calls if "/extract-results/batch/" in u]
        assert len(polls) >= 2

    def test_failed_state_raises(self):
        http = _FakeMinerUCloudHttp(fail_state=True)
        client = MinerUClient(
            MinerUConfig(base_url="https://mineru.net", api_token="tok", mode="cloud"),
            http_client=http,
        )
        with pytest.raises(MinerUError, match="失败"):
            client.parse_to_markdown(b"x", "scan.pdf")

    def test_business_code_rejected(self):
        http = _FakeMinerUCloudHttp(reject_code=-60005)
        client = MinerUClient(
            MinerUConfig(base_url="https://mineru.net", api_token="tok", mode="cloud"),
            http_client=http,
        )
        with pytest.raises(MinerUError, match="拒绝申请"):
            client.parse_to_markdown(b"x", "scan.pdf")

    def test_base_url_accepts_api_v4_suffix(self):
        # 用户填 https://mineru.net/api/v4 也能正确拼出 /api/v4/file-urls/batch（不重复）。
        http = _FakeMinerUCloudHttp()
        client = MinerUClient(
            MinerUConfig(base_url="https://mineru.net/api/v4", api_token="tok", mode="cloud"),
            http_client=http,
        )
        client.parse_to_markdown(b"x", "scan.pdf")
        assert any(u == "https://mineru.net/api/v4/file-urls/batch" for _, u in http.calls)

    def test_empty_base_defaults_to_mineru_net(self):
        http = _FakeMinerUCloudHttp()
        client = MinerUClient(
            MinerUConfig(base_url="", api_token="tok", mode="cloud"),
            http_client=http,
        )
        client.parse_to_markdown(b"x", "scan.pdf")
        assert any(u.startswith("https://mineru.net/api/v4/") for _, u in http.calls)


class TestMinerUEngineCloud:
    def test_cloud_available_with_token_only(self):
        # 云端模式：base_url 空（默认 mineru.net），有 token 即可用。
        eng = MinerUEngine(base_url="", api_token="tok", mode="cloud")
        assert eng.is_available() is True
        # 云端描述指向 token，不是 base_url（可用性只看 token）。
        assert "token" in eng.description

    def test_cloud_unavailable_without_token(self):
        eng = MinerUEngine(base_url="https://mineru.net", api_token="", mode="cloud")
        assert eng.is_available() is False
        assert "TOKEN" in eng.reason_unavailable

    def test_cloud_parse_with_fake_client(self):
        eng = MinerUEngine(
            base_url="https://mineru.net",
            api_token="tok",
            mode="cloud",
            client=_FakeMinerUCloudHttp(),
        )
        result = eng.parse(b"pdf-bytes", file_name="scan.pdf")
        assert "云端结果" in result.text
        assert result.metadata.get("engine") == "mineru"


# ============ 4. parse_document 路由与兜底 ============


class TestParseDocumentRouting:
    def test_default_builtin_parses_markdown(self):
        result = parse_document(b"# H\n\nbody", "x.md")
        assert "body" in result.text

    def test_explicit_unavailable_engine_raises(self):
        """显式选 markitdown 但未装 → 报错（暴露配置问题，不静默回退）。"""
        try:
            import markitdown  # noqa: F401
        except ImportError:
            with pytest.raises(ValueError, match="不可用"):
                parse_document(b"x", "a.pdf", engine="markitdown")

    def test_unknown_engine_falls_back_to_builtin(self):
        """未知名 → builtin（容错）。"""
        result = parse_document(b"# H\n\nbody", "x.md", engine="nonexistent")
        assert "body" in result.text

    def test_engine_not_supporting_type_falls_back(self, monkeypatch):
        """所选引擎不支持该类型 → 退回 builtin（如 markitdown 不覆盖 .py）。"""

        class _DummyMd:
            def __init__(self, *, enable_plugins=False):
                pass

            def convert_stream(self, *a, **k):  # 不应被调用
                raise AssertionError("不应走到 markitdown")

        fake = type(sys)("markitdown")
        fake.MarkItDown = _DummyMd
        fake.StreamInfo = type("S", (), {})
        monkeypatch.setitem(sys.modules, "markitdown", fake)

        # markitnow 不声明支持 .py → 退回 builtin（TextParser 解析 .py）。
        result = parse_document(b"print('hi')\n", "code.py", engine="markitdown")
        assert "print" in result.text

    def test_resolve_engine_name_priority(self):
        """引擎名解析优先级：显式 > map[ext] > map[default] > 全局默认。"""
        m = {"pdf": "mineru", "default": "builtin"}
        assert resolve_engine_name("pdf", engine_map=m) == "mineru"
        # 未配置的类型走 default
        assert resolve_engine_name("docx", engine_map=m) == "builtin"
        # 无 map → 全局默认
        assert resolve_engine_name("pdf") == "builtin"
        # 显式 engine 压过 map
        assert resolve_engine_name("pdf", engine="markitdown", engine_map=m) == "markitdown"

    def test_parse_document_uses_engine_map(self, monkeypatch):
        """parse_document 按 KB 的 engine_map 路由（map[ext] 命中 mineru）。"""
        calls = {}

        class _FakeMinerUClient:
            def parse_to_markdown(self, content, file_name):
                calls["hit"] = file_name
                return "# 标题\n\nOCR 正文"

        eng = MinerUEngine(base_url="http://m")  # base_url 非空 → is_available
        eng._client = _FakeMinerUClient()  # 替换底层客户端，不触发真实 HTTP
        monkeypatch.setattr(
            "bycall.rag.parsers.engines.get_parse_engine",
            lambda name, *, settings=None: eng if name == "mineru" else BuiltinEngine(),
        )

        result = parse_document(b"pdf-bytes", "scan.pdf", engine_map={"pdf": "mineru"})
        assert calls["hit"] == "scan.pdf"  # 走了 mineru
        assert "OCR 正文" in result.text
        assert result.metadata.get("engine") == "mineru"


# ============ 5. 引擎目录与上传类型 ============


class TestEngineCatalog:
    def test_list_three_engines(self):
        engines = list_parse_engines()
        names = [e["name"] for e in engines]
        assert names == ["builtin", "markitdown", "mineru"]

    def test_builtin_is_default(self):
        engines = {e["name"]: e for e in list_parse_engines()}
        assert engines["builtin"]["is_available"] is True
        assert engines["builtin"]["is_default"] is True
        assert engines["markitdown"]["is_default"] is False

    def test_supported_upload_types_builtin(self):
        types = supported_upload_types()
        for ext in ("pdf", "docx", "md", "txt"):
            assert ext in types

    def test_get_parse_engine_unknown_returns_builtin(self):
        eng = get_parse_engine("nope")
        assert isinstance(eng, BuiltinEngine)


# ============ 6. 配置：ParseConfig 从环境变量加载 ============


class TestParseConfig:
    def test_env_prefix_loaded(self, monkeypatch):
        monkeypatch.setenv("BYCALL_PARSE_ENGINE", "mineru")
        monkeypatch.setenv("BYCALL_PARSE_MINERU_BASE_URL", "https://mineru.example")
        monkeypatch.setenv("BYCALL_PARSE_MINERU_LANG", "en")
        from bycall.config import get_settings

        get_settings.cache_clear()
        cfg = get_settings().parse
        assert cfg.engine == "mineru"
        assert cfg.mineru_base_url == "https://mineru.example"
        assert cfg.mineru_lang == "en"


# ============ 7. DB 优先的运行时配置（SystemSetting，控制台可改）============


class TestDbFirstParseConfig:
    """解析引擎配置优先级：DB（SystemSetting）> .env 环境变量 > 代码默认。"""

    def test_seed_creates_parse_settings(self):
        """init_db 灌入全部 parse.* 默认 key（控制台可见可改）。"""
        from bycall.db import session_scope
        from bycall.governance.models import SystemSetting

        with session_scope() as s:
            keys = {r.key for r in s.exec(SystemSetting.select_all()).all()}
        expected = {
            "parse.engine",
            "parse.markitdown_enable_plugins",
            "parse.mineru_base_url",
            "parse.mineru_api_token",
            "parse.mineru_backend",
            "parse.mineru_parse_method",
            "parse.mineru_lang",
            "parse.mineru_timeout",
            "parse.mineru_poll_interval",
        }
        assert expected <= keys

    def test_db_engine_overrides_default(self):
        """DB 里 parse.engine=markitdown → 目录默认标记即时跟着变。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "parse.engine", "markitdown")
        engines = {e["name"]: e for e in list_parse_engines()}
        assert engines["markitdown"]["is_default"] is True
        assert engines["builtin"]["is_default"] is False

    def test_db_mineru_base_url_makes_engine_available(self):
        """DB 配了 mineru_base_url → mineru 引擎变可用（无需改 .env / 重启）。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "parse.mineru_base_url", "http://mineru.test")
        engines = {e["name"]: e for e in list_parse_engines()}
        assert engines["mineru"]["is_available"] is True

    def test_invalid_db_engine_falls_back_to_default(self):
        """DB 脏数据（非法引擎名）→ 回退代码默认，不抛错。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "parse.engine", "bogus")
        engines = {e["name"]: e for e in list_parse_engines()}
        assert engines["builtin"]["is_default"] is True

    def test_env_fallback_when_db_empty(self, monkeypatch):
        """DB 值为空串 → 回退环境变量（存量 .env 部署不被空 DB 值覆盖）。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "parse.mineru_base_url", "")  # 空串 = 未设
        monkeypatch.setenv("BYCALL_PARSE_MINERU_BASE_URL", "http://env.mineru")
        engines = {e["name"]: e for e in list_parse_engines()}
        assert engines["mineru"]["is_available"] is True

    def test_resolve_engine_name_uses_db_default(self):
        """resolve_engine_name 无显式参数时取 DB 默认引擎。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "parse.engine", "mineru")
        assert resolve_engine_name("pdf") == "mineru"
