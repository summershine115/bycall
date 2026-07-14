"""FastAPI 应用工厂。

纯 API 后端 + 托管 SPA 打包产物（前后端分离，前端在独立 frontend/ 子项目）。
- 控制面（admin REST API）：受会话守卫保护。
- 数据面（/chat 治理脊柱）：证明 LLM 拿不到它无权拿的东西。
- SPA 托管：开发时 Vite dev server（:5173）代理到 :8000；生产时 FastAPI 托管打包后的静态文件。

启动时 ``init_db()`` 建表 + seed 默认 admin/角色/提示词。
"""

# FastAPI 的 Depends(...)/Form(...) 作为参数默认值是框架惯用法，与 ruff B008 冲突，整文件豁免。
# ruff: noqa: B008

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from bycall import __version__
from bycall.api.routes import health
from bycall.config import get_settings
from bycall.db import get_session, init_db
from bycall.governance.auth import load_session_cookie, login, make_session_cookie

logger = logging.getLogger(__name__)

SESSION_COOKIE = "bycall_session"


def create_app() -> FastAPI:
    """构造并返回 FastAPI 实例，挂载控制面 + 数据面路由 + SPA 托管。"""
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    # 启动即建表 + seed（dev/首次部署用；生产可用迁移工具替代）。
    init_db()

    app = FastAPI(
        title="Bycall",
        description="企业级数字员工 Agent（控制面 + 数据面）",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )

    # ---- 健康检查（无认证）----
    app.include_router(health.router, tags=["health"])

    # ---- 会话登录/登出 ----
    _register_session_routes(app)

    # ---- 控制面：admin API（基于会话守卫）----
    from bycall.admin.api import router as admin_router

    app.include_router(admin_router, dependencies=[Depends(_require_admin_session)])

    # ---- 数据面：/chat 治理脊柱 ----
    from bycall.api.dataplane import router as dataplane_router

    app.include_router(dataplane_router)

    # ---- SPA 托管（生产用：托管 frontend/dist 打包产物）----
    _mount_spa(app)

    # ---- IM 渠道（启动时若已启用且有凭证则建连；admin 可在控制台热重载）----
    from bycall.gateway.manager import get_channel_manager

    mgr = get_channel_manager()

    @app.on_event("startup")
    def _start_im() -> None:
        mgr.start_if_enabled()

    @app.on_event("shutdown")
    def _stop_im() -> None:
        mgr.stop()

    logger.info("Bycall app created (env=%s, version=%s)", settings.env, __version__)
    return app


def _setup_im_channel_deprecated() -> None:
    """已废弃：改用 ChannelManager（gateway/manager.py）支持热重载。"""
    raise NotImplementedError


# ============ 会话路由 ============


def _register_session_routes(app: FastAPI) -> None:
    """登录/登出。cookie 用 itsdangerous 签名，httpOnly + (prod)secure。"""

    @app.post("/api/login", tags=["session"])
    def login_route(
        response: Response,
        name: str = Form(...),
        password: str = Form(...),
        session: Session = Depends(get_session),
    ) -> dict[str, bool]:
        """admin 登录。成功设置签名 cookie（httpOnly）。"""
        data = login(session, name, password)
        if data is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号或密码错误")
        response.set_cookie(
            SESSION_COOKIE,
            make_session_cookie(data),
            httponly=True,
            samesite="lax",
            secure=get_settings().env == "prod",
            max_age=86400,
        )
        return {"ok": True}

    @app.post("/api/logout", tags=["session"])
    def logout_route(response: Response) -> dict[str, bool]:
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}


def _require_admin_session(request: Request) -> None:
    """admin 守卫：每请求重查 DB，校验签名 cookie + 当前用户仍是 active admin。

    不能只信 cookie 里缓存的 is_admin——admin 被禁用 / 改密 / 降级为 member 后，
    旧 cookie 必须立即失效。每次请求重查 DB 校验：
    1. 用户存在且 is_active=True
    2. role=='admin'
    3. password_version（password_hash[:8]）与 cookie 一致——改密后旧会话失效
    任一不满足 → 401（让前端重新登录）。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    data = load_session_cookie(token)
    if data is None or not data.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")

    # 每请求重查 DB：确认当前仍是 active admin 且密码未改。
    from bycall.governance.models import User

    session_gen = get_session()
    db = next(session_gen)
    try:
        user = db.get(User, data.user_id)
    finally:
        db.close()

    if (
        user is None
        or not user.is_active
        or user.role != "admin"
        or (user.password_hash[:8] if user.password_hash else "") != data.password_version
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话已失效，请重新登录")


def _mount_spa(app: FastAPI) -> None:
    """挂载 SPA 打包产物（frontend/dist）。不存在则跳过（开发模式用 Vite dev server）。

    生产部署：``cd frontend && npm run build`` 生成 dist/，FastAPI 在根路径托管。
    SPA history 模式：所有非 API/非静态路径 fallback 到 index.html。
    """
    dist = _frontend_dist_dir()
    if not dist.exists():
        logger.info("frontend/dist 不存在，跳过 SPA 托管（开发模式用 Vite dev server :5173）")
        return

    # 静态资源（JS/CSS/图片）走 /assets
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="spa-assets")

    # SPA fallback：非 API 路径返回 index.html（history 模式路由）
    index_html = (dist / "index.html").read_text(encoding="utf-8")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # API/health/docs 路径不走 SPA
        if full_path.startswith(("api/", "health", "ready", "docs", "openapi", "assets/")):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return Response(content=index_html, media_type="text/html")


def _frontend_dist_dir() -> Path:
    """frontend/dist 的路径（仓库根下）。"""
    return Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
