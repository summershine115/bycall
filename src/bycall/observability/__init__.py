"""可观测性入口（M5）：OpenTelemetry trace + 结构化日志（correlation_id 贯穿治理链路）。

默认关闭：``setup_observability(enabled=False)`` 完全 no-op——不注册 middleware、
不配 logging、不配 tracing，create_app 行为与现状一致，现有测试零影响。

启用方式（生产）：
    BYCALL_OTEL_ENABLED=true
    BYCALL_OTEL_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces

on-call 问题（指导信号设计）：
- Q1 响应慢在哪一跳？→ trace（治理链路 capability/RAG/LLM/tool 各步 span + 耗时）
- Q2 治理决策路径如何复现？→ correlation_id 贯穿日志/trace/审计
- Q3 RAG 召回 0 是 ACL 过滤还是无命中？→ span 带 retrieved/decision 属性
"""

from __future__ import annotations

import logging

from bycall.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from bycall.observability.logging_setup import setup_logging
from bycall.observability.middleware import AccessLogMiddleware, CorrelationMiddleware
from bycall.observability.tracing import get_tracer, setup_tracing, traced

logger = logging.getLogger(__name__)

__all__ = [
    "AccessLogMiddleware",
    "CorrelationMiddleware",
    "get_correlation_id",
    "get_tracer",
    "new_correlation_id",
    "reset_correlation_id",
    "set_correlation_id",
    "setup_logging",
    "setup_observability",
    "setup_tracing",
    "traced",
]


def setup_observability(
    *,
    enabled: bool,
    service_name: str,
    otlp_endpoint: str,
    sampler_ratio: float,
    structured_logs: bool,
    log_level: str = "INFO",
) -> None:
    """统一初始化入口。enabled=False → 完全 no-op（默认）。

    enabled 时：先配结构化日志（correlation filter + formatter），再配 OTel tracing。
    middleware 与 auto-instrument 由 app 接线层在 setup 后触发（需 app/engine 实例）。
    """
    if not enabled:
        return

    setup_logging(structured=structured_logs, level=log_level)
    setup_tracing(
        enabled=True,
        service_name=service_name,
        otlp_endpoint=otlp_endpoint,
        sampler_ratio=sampler_ratio,
    )
    logger.info("可观测性已启用（OTel trace + 结构化日志）")
