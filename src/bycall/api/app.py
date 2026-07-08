"""FastAPI 应用工厂。

用工厂函数（而非模块级全局 app）便于：
- 测试里用 TestClient 独立构造，避免单例污染。
- 部署时按 env 注入不同配置（dev/prod）。
- ``make dev`` 用 ``uvicorn ... --factory`` 启动。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bycall import __version__
from bycall.api.routes import health
from bycall.config import get_settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """构造并返回 FastAPI 实例，挂载所有路由。"""
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    app = FastAPI(
        title="Bycall",
        description="企业级数字员工 Agent",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )

    # 注册路由
    app.include_router(health.router, tags=["health"])

    logger.info("Bycall app created (env=%s, version=%s)", settings.env, __version__)
    return app
