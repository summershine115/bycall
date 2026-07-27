"""结构化日志：correlation_id filter + JSON/可读 formatter。

设计：
- ``CorrelationFilter`` 给每条 LogRecord 注入 ``correlation_id``（取自 contextvars，无则 "-"）。
- formatter：``structured=True`` → JSON 行（生产日志聚合用）；``False`` → 人类可读带 [cid] 前缀（开发）。
- 现有 ``logger.info("中文 %s", arg)`` 调用零改动——只增强输出格式，不重写调用点。

仅在 ``BYCALL_OTEL_ENABLED=true`` 时由 setup_observability 触发；未启用时根 logger
仍用 basicConfig 默认格式，行为与现状一致。
"""

from __future__ import annotations

import json
import logging

from bycall.observability.correlation import get_correlation_id


class CorrelationFilter(logging.Filter):
    """给每条 LogRecord 注入 correlation_id（取自 contextvars，无则 "-"）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """生产用 JSON 行：ts/level/logger/msg/correlation_id，便于日志聚合查询。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ReadableFormatter(logging.Formatter):
    """开发用：``[cid] LEVEL name: msg``，correlation_id 短前缀便于肉眼关联。"""

    def __init__(self) -> None:
        super().__init__(fmt="[%(correlation_id)s] %(levelname)s %(name)s: %(message)s")


def setup_logging(*, structured: bool, level: str = "INFO") -> None:
    """配置根 logger：挂 correlation filter + formatter。

    幂等：清掉现有 handler 再挂，避免重复（basicConfig 已挂的也清）。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter() if structured else ReadableFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
