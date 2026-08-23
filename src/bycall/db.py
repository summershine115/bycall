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

    # auditlog: 补 before / after / tool_results 列
    if "auditlog" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("auditlog")}
        missing = {"before", "after", "tool_results", "context_trace"} - columns
        if missing:
            with engine.begin() as connection:
                for column in sorted(missing):
                    connection.execute(
                        text(
                            f"ALTER TABLE auditlog ADD COLUMN \"{column}\" VARCHAR NOT NULL DEFAULT ''"
                        )
                    )

    # knowledgebase: 补切块配置等新增列（chunk_size 等）
    if "knowledgebase" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("knowledgebase")}
        # (列名, DDL 类型+默认值)
        kb_migrations = [
            ("chunk_size", "INTEGER NOT NULL DEFAULT 500"),
            ("chunk_overlap", "INTEGER NOT NULL DEFAULT 50"),
            (
                "separators",
                'VARCHAR NOT NULL DEFAULT \'["\\n\\n","\\n","。","！","？","；",".","!","?",";","，",","," "]\'',
            ),
            ("strategy", "VARCHAR NOT NULL DEFAULT 'auto'"),
            ("enable_parent_child", "BOOLEAN NOT NULL DEFAULT 0"),
            ("parent_chunk_size", "INTEGER NOT NULL DEFAULT 4096"),
            ("child_chunk_size", "INTEGER NOT NULL DEFAULT 384"),
            ("total_chars", "INTEGER NOT NULL DEFAULT 0"),
            # per-KB 解析引擎映射（JSON）；nullable——升级前已存在的 KB 为 NULL，代码当 {} 处理。
            ("parse_engine_map", "VARCHAR"),
        ]
        missing = [(name, ddl) for name, ddl in kb_migrations if name not in columns]
        if missing:
            with engine.begin() as connection:
                for name, ddl in missing:
                    connection.execute(text(f'ALTER TABLE knowledgebase ADD COLUMN "{name}" {ddl}'))

    # approvalrequest: 补 im_user_id 列（M6 审批结果主动推送目标）
    if "approvalrequest" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("approvalrequest")}
        if "im_user_id" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE approvalrequest ADD COLUMN \"im_user_id\" VARCHAR NOT NULL DEFAULT ''"
                    )
                )
        # S1b：补 task_id / step_id 列（任务内审批 → approve/reject 后恢复任务）
        if "task_id" not in columns:
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE approvalrequest ADD COLUMN "task_id" INTEGER'))
        if "step_id" not in columns:
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE approvalrequest ADD COLUMN "step_id" INTEGER'))
        # T3.3：补 grant_id 列（授权免审执行时记录命中的 ApprovalGrant.id，可追溯）
        if "grant_id" not in columns:
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE approvalrequest ADD COLUMN "grant_id" INTEGER'))

    # skill: 补 disclosure 列（渐进式披露：always / on_demand）
    if "skill" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("skill")}
        if "disclosure" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE skill ADD COLUMN \"disclosure\" VARCHAR NOT NULL DEFAULT 'always'"
                    )
                )

    # conversation: 补 summary_covered_count / tenant_id / agent_id 列（会话键多租户预留 T1.5）
    if "conversation" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("conversation")}
        if "summary_covered_count" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        'ALTER TABLE conversation ADD COLUMN "summary_covered_count" '
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                )
        if "tenant_id" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE conversation ADD COLUMN \"tenant_id\" VARCHAR NOT NULL DEFAULT 'default'"
                    )
                )
        if "agent_id" not in columns:
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE conversation ADD COLUMN "agent_id" INTEGER'))

    # document: 补 level 列（T1.1 密级/职级 ABAC：文档密级，检索按 level <= user_level 过滤）。
    if "document" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("document")}
        if "level" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text('ALTER TABLE document ADD COLUMN "level" INTEGER NOT NULL DEFAULT 0')
                )

    # usermemory: 补 source / confidence / status 列（S2 记忆层 Phase 1）。存量记忆均为
    # admin 维护、可信、有效，默认值（'admin' / 1.0 / 'active'）对齐现状，补列零成本。
    if "usermemory" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("usermemory")}
        memory_migrations = [
            ("source", "VARCHAR NOT NULL DEFAULT 'admin'"),
            ("confidence", "FLOAT NOT NULL DEFAULT 1.0"),
            ("status", "VARCHAR NOT NULL DEFAULT 'active'"),
        ]
        missing = [(name, ddl) for name, ddl in memory_migrations if name not in columns]
        if missing:
            with engine.begin() as connection:
                for name, ddl in missing:
                    connection.execute(text(f'ALTER TABLE usermemory ADD COLUMN "{name}" {ddl}'))


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
                        "你是 Bycall 数字员工，企业内部同事的协作助手。\n"
                        "\n"
                        "【对话方式】\n"
                        "- 像一位靠谱、简洁的同事那样自然交流：该寒暄寒暄，该办事办事，不机械、不端着。\n"
                        "- 认识对方时自然体现（可用名字称呼），但不要逐条罗列其用户名、角色、职级或权限，除非对方明确询问。\n"
                        "- 紧扣对方的问题回应；闲聊或问候就用自然的人话，不要把系统上下文当资料汇报。\n"
                        "\n"
                        "【回答原则】\n"
                        "- 基于提供的知识库内容和已授权工具回答；不知道就明确说不知道，不编造。\n"
                        "- 简洁、专业、对用户有帮助；该详细时详细，该一句话时一句话。\n"
                        "- 不得声称拥有或执行系统未授予你的工具、知识或权限。"
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
        # IM 配置保留在 SystemSetting（渠道凭证，非模型）
        _seed_setting(session, "im.enable", "false", "是否启用 IM 渠道长连接")
        _seed_setting(session, "im.wecom_bot_id", "", "企微智能机器人 ID")
        _seed_setting(session, "im.wecom_bot_secret", "", "企微智能机器人 Secret", is_secret=True)
        # M6：企微长连接可靠性参数（非敏感）。默认无限重连，避免渠道连续失败 10 次后静默死亡。
        _seed_setting(
            session, "im.wecom_max_reconnect_attempts", "-1", "最大重连次数（-1=无限重连）"
        )
        _seed_setting(
            session, "im.wecom_reconnect_interval_ms", "1000", "重连基础延迟（毫秒，指数退避）"
        )
        _seed_setting(session, "im.wecom_heartbeat_interval_ms", "30000", "心跳间隔（毫秒）")
        # S2 记忆层 Phase 2：agent 自动记录记忆的开关与保守阈值（控制台可调，保存即时生效）。
        _seed_setting(
            session, "memory.agent_extract_enabled", "true", "是否启用 agent 自动记忆提取"
        )
        _seed_setting(
            session,
            "memory.agent_confidence_threshold",
            "0.85",
            "agent 记忆提取置信阈值（0~1，低于此值不写）",
        )
        # T2.5 成本指标单价（每百万 token 美元）：运营看板成本估算用，非计费系统。
        _seed_setting(
            session,
            "cost.chat_input_usd_per_1m",
            "0.15",
            "chat 模型输入 token 单价（美元/百万）",
        )
        _seed_setting(
            session,
            "cost.chat_output_usd_per_1m",
            "0.60",
            "chat 模型输出 token 单价（美元/百万）",
        )
        # 解析引擎配置与 IM 凭证同机制：控制台可编辑、保存即时生效
        # （DB > .env 环境变量 > 代码默认）。已部署环境从 .env 回填默认值。
        _seed_parse_settings(session)


def _seed_setting(
    session, key: str, value: str, description: str, *, is_secret: bool = False
) -> None:
    """灌一个默认设置（幂等：key 已存在则跳过）。"""
    from bycall.governance.models import SystemSetting
    from bycall.governance.settings_store import set_setting_value

    if session.exec(SystemSetting.select_by_key(key)).first() is None:
        set_setting_value(session, key, value, is_secret=is_secret, description=description)


def _seed_parse_settings(session) -> None:
    """灌解析引擎默认设置（幂等）。

    首次部署取代码默认；已有 .env 配置的存量部署取环境变量值回填（避免控制台
    显示空值导致运维困惑），之后以 DB 为第一真相源。
    """
    import os

    p = get_settings().parse

    def _default(key_suffix: str, code_default: object) -> str:
        """环境变量有值 → 回填；否则代码默认（bool 统一小写，前端按 'true'/'false' 匹配）。"""
        env = os.environ.get(f"BYCALL_PARSE_{key_suffix.upper()}")
        if env:
            return env
        return str(code_default).lower() if isinstance(code_default, bool) else str(code_default)

    _seed_setting(
        session,
        "parse.engine",
        _default("ENGINE", p.engine),
        "默认解析引擎：builtin / markitdown / mineru",
    )
    _seed_setting(
        session,
        "parse.markitdown_enable_plugins",
        _default("MARKITDOWN_ENABLE_PLUGINS", p.markitdown_enable_plugins),
        "MarkItDown 是否启用第三方插件",
    )
    _seed_setting(
        session,
        "parse.mineru_mode",
        _default("MINERU_MODE", p.mineru_mode),
        "MinerU 部署模式：selfhost（自建 /tasks）/ cloud（mineru.net /api/v4）",
    )
    _seed_setting(
        session,
        "parse.mineru_base_url",
        _default("MINERU_BASE_URL", p.mineru_base_url),
        "MinerU 服务基地址（selfhost：自建根，如 http://127.0.0.1:8000；cloud：mineru.net 根，空→默认 mineru.net）",
    )
    _seed_setting(
        session,
        "parse.mineru_api_token",
        _default("MINERU_API_TOKEN", ""),
        "MinerU 鉴权 token（Bearer）；selfhost 多数可留空，cloud（mineru.net）必填",
        is_secret=True,
    )
    _seed_setting(
        session,
        "parse.mineru_backend",
        _default("MINERU_BACKEND", p.mineru_backend),
        "MinerU 解析后端（selfhost 专用）：pipeline / vlm-auto-engine / vlm-http-client / hybrid-auto-engine / hybrid-http-client",
    )
    _seed_setting(
        session,
        "parse.mineru_parse_method",
        _default("MINERU_PARSE_METHOD", p.mineru_parse_method),
        "MinerU 解析方法（selfhost 专用）：auto / txt / ocr（仅 pipeline/hybrid 生效）",
    )
    _seed_setting(
        session,
        "parse.mineru_model_version",
        _default("MINERU_MODEL_VERSION", p.mineru_model_version),
        "MinerU 模型版本（cloud 专用）：pipeline / vlm（推荐） / MinerU-HTML",
    )
    _seed_setting(
        session,
        "parse.mineru_is_ocr",
        _default("MINERU_IS_OCR", p.mineru_is_ocr),
        "MinerU 是否强制 OCR（cloud 专用，is_ocr）；默认 false 自动识别扫描件",
    )
    _seed_setting(
        session,
        "parse.mineru_lang",
        _default("MINERU_LANG", p.mineru_lang),
        "MinerU OCR 语言组：ch / en / korean / japan（selfhost pipeline/hybrid 与 cloud pipeline/vlm 均生效）",
    )
    _seed_setting(
        session,
        "parse.mineru_timeout",
        _default("MINERU_TIMEOUT", p.mineru_timeout),
        "MinerU 单任务最长等待秒（远程解析较慢）",
    )
    _seed_setting(
        session,
        "parse.mineru_poll_interval",
        _default("MINERU_POLL_INTERVAL", p.mineru_poll_interval),
        "MinerU 轮询任务状态间隔秒",
    )
