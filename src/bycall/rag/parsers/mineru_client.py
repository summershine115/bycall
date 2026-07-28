"""MinerU 远程解析客户端——对接 MinerU API 的异步任务协议。

协议（核实自 opendatalab/MinerU ``mineru/cli/fast_api.py`` + ``api_client.py``）：

1. ``POST {base_url}/tasks``：multipart 上传文件 + 表单参数 → ``202`` →
   ``{task_id, status_url, result_url}``。
2. 轮询 ``GET status_url``：``status`` ∈ {pending, processing, completed, failed}，
   ``completed`` 即结束，``failed`` 抛错。
3. ``GET result_url``（``response_format_zip=true`` 时）→ ``application/zip``，
   解压后取 ``*.md``（优先 ``full.md``，否则按文件名拼接）。

鉴权：``mineru_api_token`` 非空时带 ``Authorization: Bearer <token>``（mineru.net 云端
必填；多数自建部署可留空）。

可测试性：``http_client`` 可注入（生产为 None 时按需自建 ``httpx.Client``），
测试注入 fake client 即可不连真实服务。
"""

from __future__ import annotations

import io
import logging
import mimetypes
import time
import zipfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 任务终态
_COMPLETED = "completed"
_FAILED = "failed"
_INFLIGHT = {"pending", "processing"}


class MinerUError(RuntimeError):
    """MinerU 解析失败（提交/轮询/取结果任一阶段）。"""


@dataclass
class MinerUConfig:
    """MinerU 客户端配置（由 ``ParseConfig`` 的 mineru_* 字段组装）。"""

    base_url: str
    api_token: str = ""
    backend: str = "hybrid-auto-engine"
    parse_method: str = "auto"
    lang: str = "ch"
    timeout: float = 600.0
    poll_interval: float = 2.0


class MinerUClient:
    """MinerU API 同步客户端：一份二进制 → Markdown 文本。

    每次调用独立建/关连接（解析是低频重操作，无需长连接复用）。
    """

    def __init__(self, config: MinerUConfig, *, http_client=None) -> None:
        self._config = config
        # 测试注入：实现 get(url, headers, timeout) / post(url, data, files, headers, timeout)
        # 且返回带 .status_code / .json() / .content / .headers 的响应对象即可。
        self._http = http_client

    def parse_to_markdown(self, content: bytes, file_name: str) -> str:
        """提交 → 轮询 → 取结果 zip → 拼接 Markdown 全文。"""
        status_url, result_url = self._submit(content, file_name)
        self._wait(status_url)
        return self._fetch_markdown(result_url)

    # ============ 内部 ============

    def _headers(self) -> dict[str, str]:
        token = self._config.api_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _submit(self, content: bytes, file_name: str) -> tuple[str, str]:
        """提交解析任务，返回 (status_url, result_url)。"""
        url = f"{self._config.base_url.rstrip('/')}/tasks"
        mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        form = {
            "lang_list": self._config.lang,
            "backend": self._config.backend,
            "parse_method": self._config.parse_method,
            "formula_enable": "true",
            "table_enable": "true",
            "return_md": "true",
            # 要 zip 结果（result_url 直接返回 application/zip，避免猜 JSON 结构）。
            "response_format_zip": "true",
            "start_page_id": "0",
            "end_page_id": "99999",
        }
        files = {"files": (file_name, io.BytesIO(content), mime)}

        resp = self._request("POST", url, data=form, files=files)
        if resp.status_code != 202:
            raise MinerUError(f"提交 MinerU 任务失败: {resp.status_code} {_detail(resp)}")

        payload = resp.json()
        status_url = payload.get("status_url")
        result_url = payload.get("result_url")
        if not isinstance(status_url, str) or not isinstance(result_url, str):
            raise MinerUError(f"MinerU 返回的任务载荷无效: {payload!r}")
        logger.debug("MinerU 任务已提交: %s", payload.get("task_id"))
        return status_url, result_url

    def _wait(self, status_url: str) -> None:
        """轮询任务状态直到 completed / failed / 超时。"""
        deadline = time.monotonic() + self._config.timeout
        while time.monotonic() < deadline:
            resp = self._request("GET", status_url)
            if resp.status_code != 200:
                raise MinerUError(f"查询 MinerU 任务状态失败: {resp.status_code} {_detail(resp)}")
            payload = resp.json()
            status = payload.get("status")
            if status == _COMPLETED:
                return
            if status == _FAILED:
                raise MinerUError(f"MinerU 任务失败: {payload!r}")
            if status in _INFLIGHT:
                time.sleep(self._config.poll_interval)
                continue
            raise MinerUError(f"MinerU 返回未知任务状态 {status!r}: {payload!r}")
        raise MinerUError("MinerU 任务超时未完成")

    def _fetch_markdown(self, result_url: str) -> str:
        """下载结果 zip 并提取 Markdown 全文。"""
        resp = self._request("GET", result_url)
        if resp.status_code != 200:
            raise MinerUError(f"下载 MinerU 结果失败: {resp.status_code} {_detail(resp)}")
        return _extract_markdown_from_zip(resp.content)

    def _request(self, method: str, url: str, *, data=None, files=None):
        """统一 HTTP 入口：注入 client 用之，否则按需建 httpx.Client。"""
        headers = self._headers()
        timeout = self._config.timeout
        if self._http is not None:
            if method == "POST":
                return self._http.post(
                    url, data=data, files=files, headers=headers, timeout=timeout
                )
            return self._http.get(url, headers=headers, timeout=timeout)
        # 生产路径：懒加载 httpx（避免 import 强依赖）。
        import httpx  # 延迟导入：builtin 解析路径完全不加载 httpx

        client_ctx = httpx.Client(timeout=timeout, follow_redirects=True)
        try:
            if method == "POST":
                return client_ctx.post(url, data=data, files=files, headers=headers)
            return client_ctx.get(url, headers=headers)
        finally:
            client_ctx.close()


def _extract_markdown_from_zip(zip_bytes: bytes) -> str:
    """从结果 zip 提取 Markdown 全文。

    MinerU 输出每个源文件一个 ``{stem}.md``，另有汇总 ``full.md``。优先取 ``full.md``；
    否则按文件名排序拼接所有 ``*.md``。带路径穿越防护（参考 MinerU safe_extract_zip）。
    """
    parts: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            name = info.filename
            # 路径穿越防护：拒绝绝对路径 / 上级引用。
            if name.startswith("/") or ".." in name.split("/"):
                continue
            if info.is_dir() or not name.lower().endswith(".md"):
                continue
            try:
                parts[name] = zf.read(info).decode("utf-8", errors="replace")
            except KeyError:
                continue

    if not parts:
        raise MinerUError("MinerU 结果 zip 中未找到 Markdown 文件")

    # 优先 full.md（MinerU 汇总文件）。
    for key in ("full.md", "Full.md"):
        if key in parts:
            return parts[key].strip()
    # 否则按文件名排序拼接（保持稳定顺序）。
    return "\n\n".join(parts[k].strip() for k in sorted(parts)).strip()


def _detail(resp) -> str:
    """从错误响应里抠出可读 detail/message。"""
    try:
        payload = resp.json()
    except Exception:  # 非 JSON 响应退回纯文本
        text = getattr(resp, "text", "").strip()
        return text or getattr(resp, "reason_phrase", "") or ""
    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            val = payload.get(key)
            if isinstance(val, str):
                return val
    return str(payload)
