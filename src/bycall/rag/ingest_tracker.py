"""入库进度跟踪器：内存态、线程安全，供前端轮询可视化入库阶段（切块→向量化→写库）。

设计要点：
- 纯内存态，**不落库**——只反映"当前这一次入库过程"的实时进度；终态（completed/failed）
  仍以 DB 的 Document.status 为准（见 api.py 的 /progress 端点合并逻辑）。
- 进程内线程安全（``threading.Lock``），后台入库工作线程写、HTTP 请求线程读。
- 进程重启即丢失（符合"实时进度"语义）；服务重启中途的 processing 文档由 /progress
  端点给出"进度不可用，可尝试重建"的提示，不会假装有进度。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class IngestProgress:
    """单个文档的入库实时进度（内存态）。

    - ``stage``：""（未开始） | chunking（切块中） | embedding（向量化中） |
      indexing（写库中） | done（完成） | failed（失败）。
    - ``percent``：前端进度条百分比，按 ``_compute_percent`` 公式计算（前端强依赖）。
    """

    document_id: int
    title: str = ""
    stage: str = ""
    total_chunks: int = 0
    processed_chunks: int = 0
    percent: int = 0
    error: str = ""


# 解析也是异步阶段（文件上传后先解析再入库）；与入库阶段一起计入"进行中"。
_ACTIVE_STAGES = frozenset({"parsing", "chunking", "embedding", "indexing"})


def _compute_percent(progress: IngestProgress) -> int:
    """进度百分比公式（前端进度条强依赖，勿随意改动）。

    - parsing（解析，含 mineru 远程 OCR 等慢引擎）→ 2
    - chunking（切块）→ 5
    - embedding（向量化）→ 10 + round(80 * processed/total)，clamp 到 [10, 90]；
      total≤0 时兜底 10（避免除零）
    - indexing（写库）→ 95
    - done（完成）→ 100
    - failed / ""（未开始）→ 保持原值不变（失败时停在卡住的位置，便于 UI 显示断点）
    """
    stage = progress.stage
    if stage == "parsing":
        # 解析（含 mineru 远程 OCR 等慢引擎）→ 2，先于切块(5)。
        return 2
    if stage == "chunking":
        return 5
    if stage == "embedding":
        total = progress.total_chunks
        if total <= 0:
            return 10
        pct = 10 + round(80 * progress.processed_chunks / total)
        return max(10, min(90, pct))
    if stage == "indexing":
        return 95
    if stage == "done":
        return 100
    # failed 或空：保持原 percent（失败时停在卡住位置）
    return progress.percent


def _copy(progress: IngestProgress) -> IngestProgress:
    """复制一份，避免调用方持有可变引用直接改 tracker 内部状态。"""
    return IngestProgress(
        document_id=progress.document_id,
        title=progress.title,
        stage=progress.stage,
        total_chunks=progress.total_chunks,
        processed_chunks=progress.processed_chunks,
        percent=progress.percent,
        error=progress.error,
    )


class IngestProgressTracker:
    """线程安全的入库进度表（doc_id → IngestProgress）。

    写（start/update/fail/finish）与读（get/active）都用同一把锁串行化，
    保证后台线程的并发更新与请求线程的并发读互不踩踏。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, IngestProgress] = {}

    def start(self, doc_id: int, title: str = "") -> None:
        """登记一次入库开始（覆盖同 doc_id 的旧记录）。"""
        with self._lock:
            self._items[doc_id] = IngestProgress(document_id=doc_id, title=title)

    def update(self, doc_id: int, **fields: object) -> None:
        """合并字段并重算 percent（管道阶段回调用）。"""
        with self._lock:
            item = self._items.get(doc_id)
            if item is None:
                # 管道回调先于 start 到达的极端情况：兜底建一条，避免丢进度。
                item = IngestProgress(document_id=doc_id)
                self._items[doc_id] = item
            for key, value in fields.items():
                if hasattr(item, key):
                    setattr(item, key, value)
            item.percent = _compute_percent(item)

    def fail(self, doc_id: int, error: str) -> None:
        """标记失败：stage=failed、记原因，percent 保持卡住位置不变。"""
        with self._lock:
            item = self._items.get(doc_id)
            if item is None:
                item = IngestProgress(document_id=doc_id)
                self._items[doc_id] = item
            item.stage = "failed"
            item.error = error

    def finish(self, doc_id: int) -> None:
        """标记完成：stage=done、percent=100。"""
        with self._lock:
            item = self._items.get(doc_id)
            if item is None:
                item = IngestProgress(document_id=doc_id)
                self._items[doc_id] = item
            item.stage = "done"
            item.percent = 100

    def get(self, doc_id: int) -> IngestProgress | None:
        """取单个文档进度（副本）；不存在返回 None。"""
        with self._lock:
            item = self._items.get(doc_id)
            return _copy(item) if item is not None else None

    def active(self) -> list[IngestProgress]:
        """所有"进行中"的文档进度（副本），按登记顺序。"""
        with self._lock:
            return [_copy(i) for i in self._items.values() if i.stage in _ACTIVE_STAGES]

    def remove(self, doc_id: int) -> None:
        """删除某文档的进度记录（完成态记录最终也会被前端停止轮询后清掉）。"""
        with self._lock:
            self._items.pop(doc_id, None)


# 模块级单例：全进程共享一张进度表（后台入库线程写、HTTP 请求线程读）。
ingest_tracker = IngestProgressTracker()
