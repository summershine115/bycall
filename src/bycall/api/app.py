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

    # ---- IM 渠道（配置 enable=True 时启动企微长连接）----
    if settings.im.enable:
        _setup_im_channel(app, settings)

    logger.info("Bycall app created (env=%s, version=%s)", settings.env, __version__)
    return app


def _setup_im_channel(app: FastAPI, settings) -> None:
    """启动 IM 渠道长连接（后台 task），应用关闭时断开。

    凭证优先读 DB 设置（admin 在控制台填），其次 .env。enable 启动时由 .env 决定
    （长连接需在启动时建立）；bot_id/secret 从 DB 读，admin 改完需重启生效。

    企微 SDK 是 asyncio 的，但 FastAPI 的 sync 路由跑在线程池里——所以 IM 渠道
    在独立后台 task + 自己的事件循环里跑（asyncio.to_thread 模式）。
    """
    import asyncio
    import threading

    from bycall.agent.im_handler import handle_im_message
    from bycall.gateway.wecom import WecomChannel
    from bycall.governance.settings_store import get_setting_value

    channel_name = settings.im.channel
    if channel_name != "wecom":
        logger.warning("IM channel=%s 暂未实现，仅支持 wecom", channel_name)
        return

    # 凭证：DB 设置优先（admin 控制台填），.env 兜底。
    bot_id = get_setting_value(
        "im.wecom_bot_id", default=settings.im.wecom_bot_id, env_var="BYCALL_IM_WECOM_BOT_ID"
    )
    bot_secret = get_setting_value(
        "im.wecom_bot_secret",
        default=settings.im.wecom_bot_secret,
        env_var="BYCALL_IM_WECOM_BOT_SECRET",
    )
    if not bot_id or not bot_secret:
        logger.warning("IM enable=True 但 wecom_bot_id/secret 为空（.env 和 DB 均未设），跳过 IM 渠道")
        return

    wecom = WecomChannel(bot_id=bot_id, secret=bot_secret)

    def _run_im_loop() -> None:
        """在独立线程跑 asyncio 事件循环驱动 IM 渠道。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(wecom.start(handle_im_message))
            loop.run_forever()
        except Exception:
            logger.exception("IM 渠道事件循环异常")

    im_thread = threading.Thread(target=_run_im_loop, name="bycall-im", daemon=True)

    @app.on_event("startup")
    def _start_im() -> None:
        im_thread.start()
        logger.info("IM 渠道已启动 (channel=wecom, bot_id=%s)", bot_id)

    @app.on_event("shutdown")
    def _stop_im() -> None:
        wecom.stop()
        logger.info("IM 渠道已停止")


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
    """admin 守卫：校验签名 cookie 且当前会话是 admin。

    替换了 admin/api.py 里的占位 ``_require_admin``——现在控制面受真实会话保护。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    data = load_session_cookie(token)
    if data is None or not data.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")


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
