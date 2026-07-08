"""FastAPI 应用骨架。

- ``app.py``：``create_app()`` 工厂（``make dev`` 用 ``--factory`` 调用它）。
- ``routes/health.py``：``/health``（存活）+ ``/ready``（就绪，含版本/配置摘要）。

M0 只暴露健康检查；M1+ 在 routes/ 下加业务路由（对话、检索等）。
"""

from bycall.api.app import create_app

__all__ = ["create_app"]
