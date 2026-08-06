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

import hmac
import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
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
        docs_url=None if settings.env == "prod" else "/docs",
        redoc_url=None,
    )

    _register_security_middleware(app)

    # M5 可观测性（默认关闭；enabled 时启用 trace + 结构化日志 + correlation middleware）。
    _setup_observability(app, settings)

    # M5 metrics（独立于 otel.enabled，始终 on）：HTTP RED + /metrics 端点。
    # MetricsMiddleware 后注册 → 最外层，覆盖所有请求（含 security middleware）。
    from bycall.observability.metrics import metrics_asgi_app
    from bycall.observability.middleware import MetricsMiddleware

    app.add_middleware(MetricsMiddleware)
    app.mount("/metrics", metrics_asgi_app())

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

    # ---- 外部事件入口（S1b）：POST /api/events/{event_source} 触发任务 ----
    from bycall.api.events import router as events_router

    app.include_router(events_router)

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

    # ---- MCP 工具网关（启动时连 enabled server + 发现工具注册进 _REGISTRY）----
    from bycall.mcp.manager import get_mcp_manager

    mcp_mgr = get_mcp_manager()

    @app.on_event("startup")
    def _load_mcp() -> None:
        from bycall.db import session_scope

        with session_scope() as sess:
            results = mcp_mgr.load_all(sess)
        for r in results:
            logger.info("MCP server '%s': %s", r["name"], r["message"])

    @app.on_event("shutdown")
    def _close_mcp() -> None:
        mcp_mgr.close_all()

    # ---- S1b：任务调度器 + 未完成任务续跑（启动即恢复，不阻塞启动）----
    from bycall.agent.scheduler import get_task_scheduler

    task_scheduler = get_task_scheduler()

    @app.on_event("startup")
    def _start_task_scheduler() -> None:
        task_scheduler.start()

    @app.on_event("startup")
    def _resume_pending_tasks() -> None:
        """启动后后台恢复 paused/running 任务（spec §3.4：进程重启可续跑）。

        放后台线程：续跑会调 LLM，可能耗时；启动路径不该被阻塞。无激活对话模型时
        跳过（模型配好后重启或手动触发）。
        """

        def _run() -> None:
            from bycall.agent.task_engine import resume_pending_tasks
            from bycall.db import session_scope
            from bycall.llm.resolver import get_chat_model

            with session_scope() as sess:
                resolved = get_chat_model(sess)
                if resolved is None:
                    logger.warning("无激活对话模型，跳过未完成任务续跑")
                    return
                _, chat_model = resolved
                resumed = resume_pending_tasks(session=sess, chat_model=chat_model)
                if resumed:
                    logger.info("启动续跑完成: %d 个任务", len(resumed))

        threading.Thread(target=_run, name="task-resume", daemon=True).start()

    @app.on_event("shutdown")
    def _stop_task_scheduler() -> None:
        task_scheduler.stop()

    logger.info("Bycall app created (env=%s, version=%s)", settings.env, __version__)
    return app


def _setup_observability(app: FastAPI, settings) -> None:
    """M5 可观测性接线：仅在 ``settings.observability.enabled`` 时生效，否则 no-op。

    注册顺序（外→内）：Correlation → AccessLog → FastAPI auto-instrument → 其余。
    correlation 最外，确保全链路（含 security middleware）都在 cid 上下文内。
    auto-instrument 覆盖 HTTP span（FastAPI）/ DB query span（SQLAlchemy）/ 出站 http（httpx = LLM/embed/rerank 调用）。
    """
    otel = settings.observability
    if not otel.enabled:
        return

    from bycall.observability import (
        AccessLogMiddleware,
        CorrelationMiddleware,
        setup_observability,
    )

    setup_observability(
        enabled=True,
        service_name=otel.service_name,
        otlp_endpoint=otel.otlp_endpoint,
        sampler_ratio=otel.sampler_ratio,
        structured_logs=otel.structured_logs,
        log_level=settings.log_level,
    )

    # auto-instrument：框架层 I/O 自动成 span，治理链路手动 span 只补业务语义。
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from bycall.db import get_engine

    FastAPIInstrumentor.instrument_app(app)
    engine = get_engine()
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    HTTPXClientInstrumentor().instrument()

    # Starlette add_middleware 后注册的在外层：先 AccessLog 再 Correlation，
    # 让 Correlation 最外（最先建 cid），AccessLog 次之（记日志时 cid 已设）。
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(CorrelationMiddleware)
    logger.info("可观测性 middleware 已注册")


def _setup_im_channel_deprecated() -> None:
    """已废弃：改用 ChannelManager（gateway/manager.py）支持热重载。"""
    raise NotImplementedError


# ============ 会话路由 ============


def _register_session_routes(app: FastAPI) -> None:
    """登录/登出。cookie 用 itsdangerous 签名，httpOnly + (prod)secure。"""

    @app.post("/api/login", tags=["session"])
    def login_route(
        request: Request,
        response: Response,
        name: str = Form(...),
        password: str = Form(...),
        session: Session = Depends(get_session),
    ) -> dict[str, bool]:
        """admin 登录。成功设置签名 cookie（httpOnly）。

        带限流：按 IP+账号 维度，5 次/15 分钟，超限 → 429（防暴力破解）。
        """
        from bycall.governance.ratelimit import (
            RATE_LIMIT_LOGIN,
            RATE_LIMIT_LOGIN_IP,
            get_limiter,
        )

        # 限流：IP + 账号维度。
        client_ip = request.client.host if request.client else "unknown"
        rate_key = f"login:{client_ip}:{name}"
        limiter = get_limiter()
        if not limiter.allow(f"login-ip:{client_ip}", **RATE_LIMIT_LOGIN_IP) or not limiter.allow(
            rate_key, **RATE_LIMIT_LOGIN
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "登录尝试过于频繁，请稍后再试")

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


def _require_admin_session(request: Request, session: Session = Depends(get_session)) -> None:
    """admin 守卫：每请求重查 DB，校验签名 cookie + 当前用户仍是 active admin。

    不能只信 cookie 里缓存的 is_admin——admin 被禁用 / 改密 / 降级为 member 后，
    旧 cookie 必须立即失效。每次请求重查 DB 校验：
    1. 用户存在且 is_active=True
    2. role=='admin'
    3. password_version（完整密码哈希的 SHA-256 指纹）与 cookie 一致——改密后旧会话失效
    任一不满足 → 401（让前端重新登录）。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    data = load_session_cookie(token)
    if data is None or not data.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")

    # 每请求重查 DB：确认当前仍是 active admin 且密码未改。
    from bycall.governance.auth import password_version
    from bycall.governance.models import User

    user = session.get(User, data.user_id)

    if (
        user is None
        or not user.is_active
        or user.role != "admin"
        or not hmac.compare_digest(password_version(user.password_hash), data.password_version)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "会话已失效，请重新登录")


def _register_security_middleware(app: FastAPI) -> None:
    """统一补 CSRF、请求体上限、安全响应头和控制面兜底审计。"""

    unsafe_methods = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # 浏览器携带会话 cookie 发起写请求时，拒绝跨站 Origin / Fetch Metadata。
        if request.method in unsafe_methods:
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin and urlsplit(origin).netloc.lower() != host.lower():
                return JSONResponse(status_code=403, content={"detail": "跨站请求已拒绝"})
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "跨站请求已拒绝"})

        # Content-Length 可在解析 JSON/multipart 前提前拒绝；流式/无长度上传仍由端点限额兜底。
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 12 * 1024 * 1024:
                    return JSONResponse(status_code=413, content={"detail": "请求体过大"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Content-Length 无效"})

        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        if get_settings().env == "prod":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")

        if request.path_params is not None:
            _audit_admin_request_if_needed(request, response.status_code, unsafe_methods)
        return response


def _audit_admin_request_if_needed(
    request: Request, status_code: int, unsafe_methods: frozenset[str]
) -> None:
    """为尚未写详细审计的控制面写操作补一条通用审计，失败尝试也记录。"""
    if not request.url.path.startswith("/api/admin/") or request.method not in unsafe_methods:
        return
    if status_code < 400 and getattr(request.state, "admin_audited", False):
        return

    try:
        from bycall.db import session_scope
        from bycall.governance.models import AuditLog

        data = load_session_cookie(request.cookies.get(SESSION_COOKIE, ""))
        with session_scope() as session:
            session.add(
                AuditLog(
                    user_id=data.user_id if data else None,
                    user_name=data.user_name if data else "unknown",
                    channel="admin",
                    action=f"{request.method.lower()}_admin_api",
                    question=request.url.path,
                    decision="allowed" if status_code < 400 else "error",
                    detail=f"status={status_code}",
                )
            )
    except Exception:
        logger.exception("控制面兜底审计写入失败")


def _mount_spa(app: FastAPI) -> None:
    """挂载 SPA 打包产物（frontend/dist）。不存在则跳过（开发模式用 Vite dev server）。

    生产部署：``cd frontend && npm run build`` 生成 dist/，FastAPI 在根路径托管。
    SPA history 模式：所有非 API/非静态路径 fallback 到 index.html。
    """
    dist = _frontend_dist_dir()
    index_path = dist / "index.html"
    if not index_path.is_file():
        logger.info("frontend/dist 不存在，跳过 SPA 托管（开发模式用 Vite dev server :5173）")
        return

    # 静态资源（JS/CSS/图片）走 /assets
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="spa-assets")

    # SPA fallback：非 API 路径返回 index.html（history 模式路由）
    index_html = index_path.read_text(encoding="utf-8")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # API/health/docs 路径不走 SPA
        if full_path.startswith(("api/", "health", "ready", "docs", "openapi", "assets/")):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return Response(content=index_html, media_type="text/html")


def _frontend_dist_dir() -> Path:
    """frontend/dist 的路径（仓库根下）。"""
    return Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
