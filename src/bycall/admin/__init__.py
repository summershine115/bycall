"""Admin 控制面 REST API。

管理组织的治理资产：用户、角色、系统提示词、审计日志。
这层接口由控制面（Admin Console）调用，Web UI 在另一个组件里基于本 API 渲染。

子模块：
- ``schemas``：API 请求/响应 pydantic schema（不落表，只是传输契约）
- ``api``：FastAPI 路由（``/api/admin/*``），全部受 admin 守卫保护

⚠️ 端点管理的是治理资产，调用方必须是已认证的 admin。M1 先用占位守卫，
集成到 app 时替换为基于 session 的真实认证依赖。
"""

from bycall.admin import api, schemas

__all__ = ["api", "schemas"]
