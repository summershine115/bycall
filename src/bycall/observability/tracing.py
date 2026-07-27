"""OpenTelemetry trace 初始化。

设计：
- ``setup_tracing`` 仅在 ``enabled=True`` 时执行；未启用完全 no-op。
- enabled 时：TracerProvider + Resource(service_name) + ParentBased(TraceIdRatioBased) 采样 +
  OTLP/HTTP exporter（otlp_endpoint 非空）或 ConsoleSpanExporter（开发兜底）。
- ``get_tracer`` 返回命名 tracer，治理链路用它创建业务 span（capability / RAG / tool_loop）。
- ``traced`` 装饰器：未 setup 时是 noop tracer，代码总写 span、未配置时自动 no-op，零侵入。
- HTTP/DB/httpx(LLM 调用) 的自动 instrument 由 app 接线层触发（需 app/engine 实例）。
"""

from __future__ import annotations

import functools
import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

logger = logging.getLogger(__name__)

# 防止重复注册 provider（测试/热重载场景）。
_SETUP_DONE = False


def setup_tracing(
    *, enabled: bool, service_name: str, otlp_endpoint: str, sampler_ratio: float
) -> None:
    """初始化全局 TracerProvider。enabled=False 时 no-op（默认）。

    幂等：enabled 时若已初始化只记 warning（OTel 不允许重复 set_tracer_provider）。
    """
    global _SETUP_DONE

    if not enabled:
        return

    if _SETUP_DONE:
        logger.warning("OTel tracing 已初始化，跳过重复 setup")
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(
        resource=resource,
        # head-based 采样：按 ratio 抽样（1.0 = 全采）；ParentBased 让子 span 跟随根采样决策。
        sampler=ParentBased(root=TraceIdRatioBased(rate=sampler_ratio)),
    )

    if otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    else:
        # 无 endpoint → console（开发兜底，能肉眼看到 span 结构）。
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _SETUP_DONE = True
    logger.info(
        "OTel tracing 已启用: service=%s endpoint=%s sampler=%.2f",
        service_name,
        otlp_endpoint or "(console)",
        sampler_ratio,
    )


def get_tracer() -> trace.Tracer:
    """返回命名 tracer，治理链路用它创建业务 span。"""
    return trace.get_tracer("bycall.governance")


def traced(name: str):
    """装饰器：给函数包 OTel 业务 span。未 setup 时是 noop tracer，零开销零侵入。

    自动记录异常 + 返回值的 ``decision``/``retrieved_chunks`` 属性——治理决策路径
    可在 trace UI 复现（on-call Q2：放行/拒绝/无访问的判定链路）。
    """
    tracer = get_tracer()

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object):
            with tracer.start_as_current_span(name) as span:
                try:
                    result = fn(*args, **kwargs)
                except Exception as exception:
                    span.record_exception(exception)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, "exception"))
                    raise
                if hasattr(result, "decision"):
                    span.set_attribute("bycall.decision", result.decision)
                if hasattr(result, "retrieved_chunks"):
                    span.set_attribute("bycall.retrieved", result.retrieved_chunks)
                return result

        return wrapper

    return deco
