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
    _ensure_schema_compatibility()
    _seed_defaults()
    _migrate_im_bindings()
    _migrate_plaintext_secrets()
    logger.info("DB initialized with default data")


def _ensure_schema_compatibility() -> None:
    """为尚未接入 Alembic 的早期部署补最小、幂等的新增列迁移。"""
    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    if "auditlog" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("auditlog")}
    missing = {"before", "after", "tool_results"} - columns
    if not missing:
        return
    with engine.begin() as connection:
        for column in sorted(missing):
            connection.execute(
                text(f"ALTER TABLE auditlog ADD COLUMN \"{column}\" VARCHAR NOT NULL DEFAULT ''")
            )


def _migrate_plaintext_secrets() -> None:
    """把升级前的明文模型/系统密钥原地迁移为 enc:v1 密文。"""
    from sqlmodel import select

    from bycall.governance.models import Model, SystemSetting
    from bycall.governance.secrets import encrypt_secret, is_encrypted_secret

    with session_scope() as session:
        for model in session.exec(select(Model)).all():
            if model.api_key and not is_encrypted_secret(model.api_key):
                model.api_key = encrypt_secret(model.api_key)
                session.add(model)
        for setting in session.exec(select(SystemSetting).where(SystemSetting.is_secret)).all():
            if setting.value and not is_encrypted_secret(setting.value):
                setting.value = encrypt_secret(setting.value)
                session.add(setting)


def _migrate_im_bindings() -> None:
    """把历史 User.im_bindings JSON 幂等迁移到带唯一约束的规范化表。"""
    from sqlmodel import select

    from bycall.governance.models import IMBinding, User

    with session_scope() as session:
        existing = {
            (row.channel, row.im_user_id): row.user_id
            for row in session.exec(select(IMBinding)).all()
        }
        for user in session.exec(select(User)).all():
            for channel, im_user_id in (user.im_bindings or {}).items():
                key = (channel, im_user_id)
                owner = existing.get(key)
                if owner is not None and owner != user.id:
                    raise RuntimeError(
                        f"检测到重复 IM 绑定 {channel}:{im_user_id}，"
                        f"同时属于 user_id={owner} 和 user_id={user.id}"
                    )
                if owner is None:
                    session.add(
                        IMBinding(
                            user_id=user.id,
                            channel=channel,
                            im_user_id=im_user_id,
                        )
                    )
                    existing[key] = user.id


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
        # 默认 admin 账号：dev 保留固定密码；prod 必须由部署方显式提供一次性引导密码。
        if session.exec(User.select_by_name("admin")).first() is None:
            from bycall.config import get_settings as _get_settings

            if _get_settings().env == "prod":
                bootstrap_password = _get_settings().bootstrap_admin_password.get_secret_value()
                if len(bootstrap_password) < 12:
                    raise RuntimeError(
                        "首次生产启动必须设置至少 12 字符的 "
                        "BYCALL_BOOTSTRAP_ADMIN_PASSWORD；创建 admin 后可移除。"
                    )
                session.add(
                    User(
                        name="admin",
                        display_name="管理员",
                        role="admin",
                        password_hash=hash_password(bootstrap_password),
                        is_active=True,
                    )
                )
                logger.warning("生产环境已创建 bootstrap admin；请立即修改密码并移除引导密码。")
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
        # 默认 Skill（示例领域知识文档，对齐 OpenClaw SKILL，供演示/参考）。
        # 不绑定到任何角色（admin 在控制台按需把 name 加进 RoleConfig.skills）。
        from bycall.governance.models import Skill

        if session.exec(Skill.select_by_name("greeting-guide")).first() is None:
            session.add(
                Skill(
                    name="greeting-guide",
                    description="问候与转人工话术指南（示例 SKILL）",
                    content=(
                        "# 问候与转人工指南\n"
                        "## 问候规范\n"
                        "- 首次交互先问候，称呼用户姓名（如已知）。\n"
                        "- 语气专业、简洁，避免过度热情。\n\n"
                        "## 转人工时机\n"
                        "- 涉及投诉、情绪激动、超出知识范围 → 主动建议转人工。\n"
                        "- 转人工话术：「这个问题我转给人工同事跟进，稍后联系您。」"
                    ),
                    is_enabled=True,
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


def _seed_setting(
    session, key: str, value: str, description: str, *, is_secret: bool = False
) -> None:
    """灌一个默认设置（幂等：key 已存在则跳过）。"""
    from bycall.governance.models import SystemSetting
    from bycall.governance.settings_store import set_setting_value

    if session.exec(SystemSetting.select_by_key(key)).first() is None:
        set_setting_value(session, key, value, is_secret=is_secret, description=description)
