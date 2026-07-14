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
        # dev 用固定 admin123（开发便利）；prod 用随机密码（避免默认凭证被利用）。
        if session.exec(User.select_by_name("admin")).first() is None:
            import secrets as _secrets

            from bycall.config import get_settings as _get_settings

            if _get_settings().env == "prod":
                # 生产：生成随机密码并打印一次（不写死、不在登录页提示）。
                random_pw = _secrets.token_urlsafe(16)
                session.add(
                    User(
                        name="admin",
                        display_name="管理员",
                        role="admin",
                        password_hash=hash_password(random_pw),
                        is_active=True,
                    )
                )
                session.commit()
                logger.warning(
                    "生产环境已创建默认 admin 账号，随机密码（仅显示一次，请立即修改）：\n"
                    "    %s\n"
                    "请登录后立即在控制台修改密码。",
                    random_pw,
                )
            else:
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
        # 默认模型实体（LLM 升级为 Model 实体，参考 WeKnora）。
        # chat 模型：OpenAI 兼容占位（admin 在控制台激活真实 provider 的模型）
        _seed_default_model(
            session,
            name="gpt-4o-mini",
            display_name="OpenAI GPT-4o-mini",
            model_type="chat",
            provider="openai",
            base_url="https://api.openai.com/v1",
        )
        # embedding 模型：OpenAI text-embedding-3-small（dim=1536）
        _seed_default_model(
            session,
            name="text-embedding-3-small",
            display_name="OpenAI text-embedding-3-small",
            model_type="embedding",
            provider="openai",
            base_url="https://api.openai.com/v1",
            embed_dimension=1536,
        )
        # IM 配置保留在 SystemSetting（渠道凭证，非模型）
        _seed_setting(session, "im.enable", "false", "是否启用 IM 渠道长连接")
        _seed_setting(session, "im.wecom_bot_id", "", "企微智能机器人 ID")
        _seed_setting(session, "im.wecom_bot_secret", "", "企微智能机器人 Secret", is_secret=True)


def _seed_default_model(
    session, name, display_name, model_type, provider, base_url, *, embed_dimension=0
) -> None:
    """灌一个默认 Model 实体（幂等：同 name+type 已存在则跳过）。默认不激活（admin 填 key 后激活）。"""
    from sqlmodel import select

    from bycall.governance.models import Model

    existing = session.exec(
        select(Model).where(Model.name == name, Model.model_type == model_type)
    ).first()
    if existing is None:
        session.add(
            Model(
                name=name,
                display_name=display_name,
                model_type=model_type,
                source="remote",
                provider=provider,
                base_url=base_url,
                embed_dimension=embed_dimension,
                is_active=False,
                description=f"默认 {model_type} 模型（请在控制台填 API key 后激活）",
            )
        )


def _seed_setting(session, key: str, value: str, description: str, *, is_secret: bool = False) -> None:
    """灌一个默认设置（幂等：key 已存在则跳过）。"""
    from bycall.governance.models import SystemSetting
    from bycall.governance.settings_store import set_setting_value

    if session.exec(SystemSetting.select_by_key(key)).first() is None:
        set_setting_value(session, key, value, is_secret=is_secret, description=description)
