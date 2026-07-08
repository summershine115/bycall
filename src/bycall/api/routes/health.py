"""健康检查路由。

- ``GET /health``：存活探针（liveness），只要进程在就 200。K8s/负载均衡用它判断要不要重启。
- ``GET /ready``：就绪探针（readiness），返回版本 + 配置摘要（后端选型），不含敏感信息。

M0 这两个端点都不依赖外部服务（postgres/redis 还没接）。
M1+ 接入依赖后，``/ready`` 应加依赖探测（DB ping / 向量库可用性）。
"""

from __future__ import annotations

from fastapi import APIRouter

from bycall import __version__
from bycall.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """存活探针：进程在即 200。"""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, object]:
    """就绪探针：返回版本 + 后端选型摘要（不暴露 key/DSN 等敏感字段）。"""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.env,
        "llm_provider": settings.llm.provider,
        "vector_store_backend": settings.vector_store.backend,
        # M1+ 在此追加依赖探测结果：db_ok / redis_ok / vector_store_ok
    }
