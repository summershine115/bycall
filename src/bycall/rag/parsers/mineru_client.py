"""MinerU 远程解析客户端——同时支持自建部署与 mineru.net 云端两种协议。

两种部署（由 ``MinerUConfig.mode`` 选择）：

**selfhost**（自建 mineru-api，即 opendatalab/MinerU ``mineru/cli/fast_api.py``）：
1. ``POST {base_url}/tasks``：multipart 上传文件 + 表单参数（backend / parse_method /
   lang_list / response_format_zip ...）→ ``202`` → ``{task_id, status_url, result_url}``。
2. 轮询 ``GET status_url``：``status`` ∈ {pending, processing, completed, failed}，
   ``completed`` 即结束，``failed`` 抛错。
3. ``GET result_url`` → ``application/zip``，解压取 ``full.md``（或拼接所有 ``*.md``）。

**cloud**（mineru.net 精准解析 API，``/api/v4``）：
1. ``POST {base}/api/v4/file-urls/batch``：JSON 申请预签名上传链接 →
   ``{code:0, data:{batch_id, file_urls:[url]}}``。
2. ``PUT file_urls[0]``：直传文件二进制（**不带 Authorization / Content-Type**，
   OSS 预签名凭证全在 URL query，带反会上传失败）。上传完成后云端自动提交解析任务。
3. 轮询 ``GET {base}/api/v4/extract-results/batch/{batch_id}``：
   ``data.extract_result[].state`` ∈ {waiting-file, pending, running, converting, done,
   failed}；``done`` 时取 ``full_zip_url``。
4. ``GET full_zip_url`` → zip，复用 selfhost 的 zip→markdown 提取。

为何云端走 batch 而非单文件接口：单文件 ``/api/v4/extract/task`` 只收 URL、不支持直传
（官方明确「该接口不支持文件直接上传」）；本地字节流无公网 URL，故用 batch 预签名上传流。

鉴权：两种模式都 ``Authorization: Bearer <token>``（selfhost 多数自建可留空；cloud 必填）。
``code`` 信封：云端所有响应 ``{code, msg, data, trace_id}``，``code != 0`` 即业务失败。

可测试性：``http_client`` 可注入（生产为 None 时按需自建 ``httpx.Client``），
注入对象只需实现 ``get / post / put`` 三方法且返回带 ``.status_code / .json() / .content``
的响应即可。
"""

from __future__ import annotations

import io
import logging
import mimetypes
import time
import zipfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---- selfhost 任务状态（fast_api status 字段）----
_SH_COMPLETED = "completed"
_SH_FAILED = "failed"
_SH_INFLIGHT = {"pending", "processing"}

# ---- cloud 任务状态（/api/v4 extract_result.state 字段）----
_CL_DONE = "done"
_CL_FAILED = "failed"
_CL_INFLIGHT = {"waiting-file", "pending", "running", "converting"}


class MinerUError(RuntimeError):
    """MinerU 解析失败（提交 / 上传 / 轮询 / 取结果任一阶段）。"""


@dataclass
class MinerUConfig:
    """MinerU 客户端配置（由 ``ParseConfig`` 的 mineru_* 字段组装）。"""

    base_url: str = ""
    api_token: str = ""
    # 部署模式：selfhost（自建 /tasks）/ cloud（mineru.net /api/v4）
    mode: str = "selfhost"
    # ---- selfhost 专用（MinerU CLI --backend / --parse_method）----
    backend: str = "hybrid-auto-engine"
    parse_method: str = "auto"
    # ---- cloud 专用（mineru.net 精准解析 model_version / is_ocr）----
    model_version: str = "vlm"
    is_ocr: bool = False
    # ---- 共用 ----
    lang: str = "ch"
    timeout: float = 600.0
    poll_interval: float = 2.0


class MinerUClient:
    """MinerU API 同步客户端：一份二进制 → Markdown 文本。

    按 ``config.mode`` 分派到自建（``/tasks``）或云端（``/api/v4``）协议。
    每次调用独立建/关连接（解析是低频重操作，无需长连接复用）。
    """

    def __init__(self, config: MinerUConfig, *, http_client=None) -> None:
        self._config = config
        # 测试注入：实现 get(url, headers, timeout) / post(...) / put(...) 且返回带
        # .status_code / .json() / .content 的响应对象即可。
        self._http = http_client

    def parse_to_markdown(self, content: bytes, file_name: str) -> str:
        """提交 → 轮询 → 取结果 zip → 拼接 Markdown 全文。"""
        if self._config.mode == "cloud":
            return _CloudProtocol(self._config, self._http).parse(content, file_name)
        return _SelfHostProtocol(self._config, self._http).parse(content, file_name)


# ============ selfhost 协议（自建 mineru-api /tasks） ============


class _SelfHostProtocol:
    """自建 mineru-api：multipart 上传 → 轮询 status_url → GET result_url 取 zip。

    协议核实自 opendatalab/MinerU ``mineru/cli/fast_api.py``。
    """

    def __init__(self, config: MinerUConfig, http) -> None:
        self._c = config
        self._http = http

    def parse(self, content: bytes, file_name: str) -> str:
        status_url, result_url = self._submit(content, file_name)
        self._wait(status_url)
        return self._fetch_markdown(result_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._c.api_token}"} if self._c.api_token else {}

    def _submit(self, content: bytes, file_name: str) -> tuple[str, str]:
        """提交解析任务，返回 (status_url, result_url)。"""
        url = f"{self._c.base_url.rstrip('/')}/tasks"
        mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        form = {
            "lang_list": self._c.lang,
            "backend": self._c.backend,
            "parse_method": self._c.parse_method,
            "formula_enable": "true",
            "table_enable": "true",
            "return_md": "true",
            # 要 zip 结果（result_url 直接返回 application/zip，避免猜 JSON 结构）。
            "response_format_zip": "true",
            "start_page_id": "0",
            "end_page_id": "99999",
        }
        files = {"files": (file_name, io.BytesIO(content), mime)}

        resp = _post_multipart(
            self._http,
            url,
            data=form,
            files=files,
            headers=self._headers(),
            timeout=self._c.timeout,
        )
        if resp.status_code != 202:
            raise MinerUError(f"提交 MinerU 任务失败: {resp.status_code} {_detail(resp)}")

        payload = resp.json()
        status_url = payload.get("status_url")
        result_url = payload.get("result_url")
        if not isinstance(status_url, str) or not isinstance(result_url, str):
            raise MinerUError(f"MinerU 返回的任务载荷无效: {payload!r}")
        logger.debug("MinerU(selfhost) 任务已提交: %s", payload.get("task_id"))
        return status_url, result_url

    def _wait(self, status_url: str) -> None:
        """轮询任务状态直到 completed / failed / 超时。"""
        deadline = time.monotonic() + self._c.timeout
        while time.monotonic() < deadline:
            resp = _get(self._http, status_url, headers=self._headers(), timeout=self._c.timeout)
            if resp.status_code != 200:
                raise MinerUError(f"查询 MinerU 任务状态失败: {resp.status_code} {_detail(resp)}")
            payload = resp.json()
            status = payload.get("status")
            if status == _SH_COMPLETED:
                return
            if status == _SH_FAILED:
                raise MinerUError(f"MinerU 任务失败: {payload!r}")
            if status in _SH_INFLIGHT:
                time.sleep(self._c.poll_interval)
                continue
            raise MinerUError(f"MinerU 返回未知任务状态 {status!r}: {payload!r}")
        raise MinerUError("MinerU 任务超时未完成")

    def _fetch_markdown(self, result_url: str) -> str:
        """下载结果 zip 并提取 Markdown 全文。"""
        resp = _get(self._http, result_url, headers=self._headers(), timeout=self._c.timeout)
        if resp.status_code != 200:
            raise MinerUError(f"下载 MinerU 结果失败: {resp.status_code} {_detail(resp)}")
        return _extract_markdown_from_zip(resp.content)


# ============ cloud 协议（mineru.net 精准解析 /api/v4） ============


def _normalize_cloud_base(base_url: str) -> str:
    """云端 API 根：接受 ``https://mineru.net`` 或 ``https://mineru.net/api/v4``，
    统一归一到 ``<root>/api/v4``，避免重复拼接。"""
    b = base_url.rstrip("/")
    if b.endswith("/api/v4"):
        b = b[: -len("/api/v4")]
    return b.rstrip("/") + "/api/v4"


class _CloudProtocol:
    """mineru.net 云端精准解析 API（``/api/v4``）。

    本地字节流无法走单文件 ``/api/v4/extract/task``（该接口只收 URL），故走 batch 预签名
    上传：申请上传链接 → PUT 上传 → 云端自动解析 → 轮询 batch 结果 → 取 full_zip_url。
    """

    def __init__(self, config: MinerUConfig, http) -> None:
        self._c = config
        self._http = http
        # base_url 空 → 默认 mineru.net 官方域名。
        self._api_base = _normalize_cloud_base(config.base_url or "https://mineru.net")

    def parse(self, content: bytes, file_name: str) -> str:
        batch_id, upload_url = self._apply_upload(file_name)
        self._upload(upload_url, content)
        zip_url = self._wait(batch_id, file_name)
        return self._fetch_markdown(zip_url)

    def _auth_headers(self, *, json_body: bool = False) -> dict[str, str]:
        # 云端精准解析必填 token。
        h: dict[str, str] = {}
        if self._c.api_token:
            h["Authorization"] = f"Bearer {self._c.api_token}"
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _apply_upload(self, file_name: str) -> tuple[str, str]:
        """申请预签名上传链接，返回 (batch_id, upload_url)。"""
        url = f"{self._api_base}/file-urls/batch"
        payload = {
            # 单文件批次：name 让云端按后缀判定类型。
            "files": [{"name": file_name, "is_ocr": self._c.is_ocr}],
            "model_version": self._c.model_version,
            "language": self._c.lang,
            "enable_formula": True,
            "enable_table": True,
        }
        resp = _post_json(
            self._http,
            url,
            json=payload,
            headers=self._auth_headers(json_body=True),
            timeout=self._c.timeout,
        )
        if resp.status_code != 200:
            raise MinerUError(f"申请 MinerU 云端上传链接失败: {resp.status_code} {_detail(resp)}")
        body = resp.json()
        if body.get("code") != 0:
            raise MinerUError(f"MinerU 云端拒绝申请上传链接: {body!r}")
        data = body.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not isinstance(batch_id, str) or not file_urls:
            raise MinerUError(f"MinerU 云端返回上传载荷无效: {body!r}")
        logger.debug("MinerU(cloud) 批量任务已建: %s", batch_id)
        return batch_id, file_urls[0]

    def _upload(self, upload_url: str, content: bytes) -> None:
        """PUT 文件到 OSS 预签名 URL。

        OSS 预签名要求：不带 Authorization、不带 Content-Type（凭证全在 URL query，
        多余头会触发签名校验失败）。官方文档亦注明「上传时无须设置 Content-Type」。
        """
        resp = _put(self._http, upload_url, content=content, headers={}, timeout=self._c.timeout)
        if resp.status_code not in (200, 201, 204):
            raise MinerUError(f"上传文件到 MinerU 云端失败: {resp.status_code} {_detail(resp)}")

    def _wait(self, batch_id: str, file_name: str) -> str:
        """轮询 batch 结果直到 done，返回 full_zip_url。"""
        url = f"{self._api_base}/extract-results/batch/{batch_id}"
        deadline = time.monotonic() + self._c.timeout
        while time.monotonic() < deadline:
            resp = _get(self._http, url, headers=self._auth_headers(), timeout=self._c.timeout)
            if resp.status_code != 200:
                raise MinerUError(f"查询 MinerU 云端任务失败: {resp.status_code} {_detail(resp)}")
            body = resp.json()
            if body.get("code") != 0:
                raise MinerUError(f"MinerU 云端查询异常: {body!r}")
            results = (body.get("data") or {}).get("extract_result") or []
            item = self._pick_result(results, file_name)
            state = item.get("state")
            if state == _CL_DONE:
                zip_url = item.get("full_zip_url")
                if not isinstance(zip_url, str):
                    raise MinerUError(f"MinerU 云端任务完成但缺 full_zip_url: {item!r}")
                return zip_url
            if state == _CL_FAILED:
                raise MinerUError(f"MinerU 云端任务失败: {item.get('err_msg') or item!r}")
            if state in _CL_INFLIGHT:
                time.sleep(self._c.poll_interval)
                continue
            raise MinerUError(f"MinerU 云端返回未知任务状态 {state!r}: {item!r}")
        raise MinerUError("MinerU 云端任务超时未完成")

    @staticmethod
    def _pick_result(results: list, file_name: str) -> dict:
        """从 batch 结果里挑出本文件项。单文件批次直接取第一个；多文件按名匹配。"""
        if not results:
            return {}
        for r in results:
            if r.get("file_name") == file_name:
                return r
        return results[0]

    def _fetch_markdown(self, zip_url: str) -> str:
        """下载结果 zip（CDN，无需鉴权）并提取 Markdown 全文。"""
        resp = _get(self._http, zip_url, headers={}, timeout=self._c.timeout)
        if resp.status_code != 200:
            raise MinerUError(f"下载 MinerU 云端结果失败: {resp.status_code} {_detail(resp)}")
        return _extract_markdown_from_zip(resp.content)


# ============ HTTP 薄封装：注入用接口，生产懒建 httpx.Client ============


def _get(http, url: str, *, headers, timeout: float):
    if http is not None:
        return http.get(url, headers=headers, timeout=timeout)
    import httpx  # 延迟导入：builtin 解析路径完全不加载 httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.get(url, headers=headers)


def _post_multipart(http, url: str, *, data, files, headers, timeout: float):
    if http is not None:
        return http.post(url, data=data, files=files, headers=headers, timeout=timeout)
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.post(url, data=data, files=files, headers=headers)


def _post_json(http, url: str, *, json, headers, timeout: float):
    if http is not None:
        return http.post(url, json=json, headers=headers, timeout=timeout)
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.post(url, json=json, headers=headers)


def _put(http, url: str, *, content: bytes, headers, timeout: float):
    if http is not None:
        return http.put(url, content=content, headers=headers, timeout=timeout)
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        # content= 传 body 且 httpx 不会自动加 Content-Type（OSS 预签名要求）。
        return client.put(url, content=content, headers=headers)


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
        for key in ("detail", "error", "message", "msg"):
            val = payload.get(key)
            if isinstance(val, str):
                return val
    return str(payload)
