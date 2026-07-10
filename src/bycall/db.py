"""数据库引擎与会话层。

dev 用 SQLite（开箱即跑，无需 docker）；生产/CI 可通过 config 切 Postgres。
``init_db()`` 建表 + 灌默认数据（管理员账号、默认角色、默认提示词）。

设计要点：
- 用 SQLModel（pydantic + SQLAlchemy 2.x），表定义即数据模型，治理领域对象直接复用。
- 会话用 FastAPI 的 ``Depends(get_session)`` 注入，便于测试替换。
- 每个表有清晰的领域职责，关系见各模型 docstring。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from bycall.config import get_settings

logger = logging.getLogger(__name__)

# 引擎懒构造（首次调 get_session 时建），便于测试里替换。
_engine = None


def get_engine():
    """构造/复用 DB 引擎。dev 用 SQLite 文件，DSN 形如 sqlite:///./data/bycall.db。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        dsn = settings.postgres.dsn
        connect_args = {"check_same_thread": False} if dsn.startswith("sqlite") else {}
        _engine = create_engine(dsn, echo=False, connect_args=connect_args)
        logger.info("DB engine ready: %s", "sqlite" if dsn.startswith("sqlite") else "postgres")
    return _engine


def set_engine(engine) -> None:
    """测试注入专用：替换全局引擎。"""
    global _engine
    _engine = engine


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：提供一个事务会话。"""
    with Session(get_engine()) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """非 FastAPI 场景（脚本/测试/seed）用的上下文管理器。

    用法：``with session_scope() as s: ...``，退出时自动提交+关闭。
    """
    with Session(get_engine()) as session:
        yield session
        session.commit()


def init_db() -> None:
    """建表 + 灌默认数据。应用启动时调一次。

    默认数据：一个 admin 账号、一个默认角色（``member``）、一条默认系统提示词。
    这些让系统首次启动即可登录使用。
    """
    # 确保所有模型类已注册到 metadata（import 即注册）。
    from bycall.governance import models as _gov_models  # noqa: F401

    SQLModel.metadata.create_all(get_engine())
    _seed_defaults()
    logger.info("DB initialized with default data")


def _seed_defaults() -> None:
    """灌默认 admin/角色/提示词/运行时设置（幂等：已存在则跳过）。"""
    from bycall.governance.models import Role, RoleConfig, SystemPrompt, User
    from bycall.governance.security import hash_password

    with session_scope() as session:
        # 默认角色：member（普通员工，最小权限）
        if session.get(Role, "member") is None:
            session.add(
                Role(
                    name="member",
                    description="普通员工（默认最小权限）",
                    config=RoleConfig(allowed_tools=[], knowledge_scope_tags=[], level=0),
                )
            )
        # 默认 admin 账号
        if session.exec(User.select_by_name("admin")).first() is None:
            session.add(
                User(
                    name="admin",
                    display_name="管理员",
                    role="admin",  # 特殊内置角色，policy 引擎识别
                    password_hash=hash_password("admin123"),
                    is_active=True,
                )
            )
        # 默认系统提示词（v1，统一一套）
        if session.get(SystemPrompt, 1) is None:
            session.add(
                SystemPrompt(
                    version=1,
                    label="默认（全公司统一）",
                    content=(
                        "你是 Bycall 数字员工，企业内部的协作助手。\n"
                        "请基于提供的知识库内容回答问题；不知道就明确说不知道，不要编造。\n"
                        "回答要简洁、专业、对用户有帮助。"
                    ),
                    is_active=True,
                )
            )
        # 默认运行时设置（LLM/IM）。值留空——admin 在控制台填，或 .env 环境变量兜底。
        _seed_setting(session, "llm.provider", "openai", "LLM 提供商: openai | glm")
        _seed_setting(session, "llm.base_url", "https://api.openai.com/v1", "LLM API base URL（可指向代理/本地）")
        _seed_setting(session, "llm.api_key", "", "LLM API key", is_secret=True)
        _seed_setting(session, "llm.model", "gpt-4o-mini", "对话模型名")
        _seed_setting(session, "llm.embed_model", "text-embedding-3-small", "向量模型名")
        _seed_setting(session, "im.enable", "false", "是否启用 IM 渠道长连接")
        _seed_setting(session, "im.wecom_bot_id", "", "企微智能机器人 ID")
        _seed_setting(session, "im.wecom_bot_secret", "", "企微智能机器人 Secret", is_secret=True)


def _seed_setting(session, key: str, value: str, description: str, *, is_secret: bool = False) -> None:
    """灌一个默认设置（幂等：key 已存在则跳过）。"""
    from bycall.governance.models import SystemSetting
    from bycall.governance.settings_store import set_setting_value

    if session.exec(SystemSetting.select_by_key(key)).first() is None:
        set_setting_value(session, key, value, is_secret=is_secret, description=description)
