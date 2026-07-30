"""治理领域模型（SQLModel 表）。

这是整个权限系统的"真值"所在——admin 在控制面修改这些记录，
数据面的每次交互都据此计算能力集。LLM 碰不到这层。

表关系：
- User ──> Role（多对一，用户属于一个角色）
- Role ──内嵌──> RoleConfig（该角色的工具白名单 / 知识域 / 职级）
- SystemPrompt（版本化，激活版本下发给数据面）
- Skill（领域知识文档，对齐 OpenClaw SKILL，admin 管控，按角色绑定 name 列表）
- AuditLog（append-only，谁/何时/问什么/放行/拒绝）
- Model（LLM/向量模型实体，每种类型一个激活）
- KnowledgeBase（知识库实体，per-KB embedding 模型 + 维度 + zvec collection）
- Document（知识库文档，属于某个 KB，source = ACL 标签）
- Conversation + Message（会话状态层，按 (user_id, channel) 隔离，参考 Hermes SessionStore）
- UserMemory（用户级长期记忆，跨会话持久，admin 管控）
- Task/Step/Run（持久化多步任务：run_agent 工具回路作 Step 内核，状态机驱动，可暂停/恢复）

⚠️ 设计约定：
- ``admin`` 是内置特殊角色名（不建 Role 表记录），policy 引擎识别后给全部权限。
- ``knowledge_scope_tags`` 是知识域标签（如 ["sales","public"]），RAG 预过滤时
  翻译成 ``source IN (...)`` 形式的 filter。M2 接 ACL 管理界面后可细化到文档级。
- ``level`` 是职级（ABAC 用），0=普通，越大权限越高（机密文件按 ``level <= user_level`` 放行）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

from bycall.governance.json_types import DictJSONType, ListJSONType, PydanticJSONType


def _now_utc() -> datetime:
    """统一的当前时间（timezone-aware UTC）。``datetime.utcnow`` 已弃用，统一用这个。"""
    return datetime.now(UTC)


# ============ RoleConfig：角色的权限配置（嵌套在 Role 里）============


class RoleConfig(BaseModel):
    """单个角色的权限配置。admin 在控制面编辑，policy 引擎读取。

    - ``allowed_tools``：该角色可用的工具名白名单。LLM 的工具列表 = 这个集合。
      不在此列表的工具，LLM 根本看不到（不是"看到不让调"）。
    - ``knowledge_scope_tags``：可见知识域标签（如 ["sales","public","hr"]）。
      RAG 预过滤时翻译成 ``source IN (...)``。
    - ``level``：职级，用于 ABAC（机密资源按 ``resource_level <= user_level`` 放行）。
    - ``system_prompt_version``：角色绑定的系统提示词版本（``SystemPrompt.version``）。
      非空时该角色用此版本提示词（而非全局激活版），实现按角色差异化 LLM 行为；
      为 None 时 fallback 全局激活版本（后向兼容，M1 行为不变）。
    - ``skills``：该角色绑定的 SKILL name 列表（对齐 OpenClaw SKILL 定义——注入
      system prompt 的领域知识文档）。capability 引擎据此加载启用的 skill 内容，
      合并成 markdown 注入 system message。未注册/已禁用的 name 静默忽略（与
      ``allowed_tools`` 一致的容错语义）。admin 内置角色不走此字段（运维角色不注入 skill）。
    """

    allowed_tools: list[str] = Field(default_factory=list)
    knowledge_scope_tags: list[str] = Field(default_factory=list)
    level: int = 0
    system_prompt_version: int | None = None
    skills: list[str] = Field(default_factory=list)


# ============ Role ============


class Role(SQLModel, table=True):
    """角色定义。一个角色 = 一套权限配置（RoleConfig）。"""

    name: str = Field(primary_key=True)  # 如 "member" / "sales" / "manager"
    description: str = ""
    # JSON 存 RoleConfig（SQLModel 原生不支持嵌套表模型用 JSON 列）。
    config: RoleConfig = Field(
        default_factory=RoleConfig, sa_column=Column("config", PydanticJSONType(RoleConfig))
    )
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )

    @staticmethod
    def select_by_name(name: str):
        """便利方法：按 name 查（db.py seed 用）。"""
        from sqlmodel import select

        return select(Role).where(Role.name == name)


# ============ User ============


class User(SQLModel, table=True):
    """用户记录。邀请制——admin 在控制面创建，员工首次 IM 接入时绑定。

    ``im_bindings`` 存 {channel: im_user_id} 映射，一个员工可能在企微和飞书
    各有一个 user_id，都映射到同一条 User 记录。
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # 登录名（admin）/ 业务标识
    display_name: str = ""
    role: str = Field(default="member", index=True)  # Role.name，或内置 "admin"
    password_hash: str = ""  # admin 登录用；普通 IM 用户为空
    im_bindings: dict[str, str] = Field(
        default_factory=dict, sa_column=Column("im_bindings", DictJSONType)
    )
    is_active: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )

    @staticmethod
    def select_by_name(name: str):
        from sqlmodel import select

        return select(User).where(User.name == name)

    @staticmethod
    def select_by_im(channel: str, im_user_id: str):
        """按 IM 渠道+用户ID 查（数据面认证用）。

        注：SQLite 下 JSON 列查询受限，这里取全部后 Python 侧过滤；
        迁 Postgres 后可用 JSONB 操作符下推。M1 量小不影响。
        """
        from sqlmodel import select

        return select(User).where(User.is_active == True)  # noqa: E712


class IMBinding(SQLModel, table=True):
    """规范化 IM 身份映射；数据库唯一约束是身份可信根的最终防线。"""

    __table_args__ = (UniqueConstraint("channel", "im_user_id", name="uq_im_identity"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    channel: str = Field(index=True)
    im_user_id: str = Field(index=True)


# ============ SystemPrompt ============


class SystemPrompt(SQLModel, table=True):
    """系统提示词版本。admin 管理，激活版本下发给数据面。

    用户消息永远只进 user message，物理上拼不到 system 之上。
    M1 全公司统一一套（is_active 的那条）；架构预留按角色分发（M2/M3）。
    """

    id: int | None = Field(default=None, primary_key=True)
    version: int = Field(unique=True)  # 版本号，递增
    label: str = ""  # 人类可读标签
    content: str  # 提示词正文
    is_active: bool = Field(default=False, index=True)  # 全局只有一条 active
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )

    @staticmethod
    def select_active():
        from sqlmodel import select

        return select(SystemPrompt).where(SystemPrompt.is_active == True)  # noqa: E712


# ============ Skill：领域知识文档（对齐 OpenClaw SKILL）============


class Skill(SQLModel, table=True):
    """领域知识文档——admin 管控的、注入 system prompt 的可迭代 Markdown。

    对齐 OpenClaw 的 SKILL 定义：SKILL ≠ Tool。Tool 是可执行函数（LLM 通过 function
    calling 调用），SKILL 是给 LLM 读的文档（注入 system prompt，指导 LLM 如何使用
    已有工具完成某领域任务）。SystemPrompt 定义"数字员工是谁"（人格/行为规则），
    Skill 定义"会什么具体业务"（领域知识/操作指南）。

    与 OpenClaw 的差异（信任模型相反）：OpenClaw 是个人 Agent，用户自己管 SKILL 文件
    （文件系统扫描）；Bycall 是组织 Agent，SKILL 由 admin 管控、存 DB、按角色分发
    （``RoleConfig.skills`` 绑定 name 列表）。

    数据模型：**可迭代文档**（非版本化不可变）——``name`` 唯一标识 + ``is_enabled`` 启停
    + ``content`` 可编辑 + ``updated_at``。admin 改 content 后下次对话即时生效（DB 即
    真值）。不走 SystemPrompt 的 version+is_active 模式（每次改发版太重，不贴合文档语义）。
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # 唯一标识，如 "sales-playbook"
    description: str = ""  # 一句话描述（admin 目录展示，不进 prompt）
    content: str  # Markdown body（领域知识，进 system prompt）
    is_enabled: bool = Field(default=True, index=True)  # 启用/禁用（非全局唯一激活）
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_enabled():
        """查全部启用的 skill（按 name 排序，输出稳定）。"""
        from sqlmodel import select

        return select(Skill).where(Skill.is_enabled == True).order_by(Skill.name)  # noqa: E712

    @staticmethod
    def select_by_name(name: str):
        """按 name 查单条。"""
        from sqlmodel import select

        return select(Skill).where(Skill.name == name)

    @staticmethod
    def select_all():
        """查全部（含禁用，按 name 排序）。admin 目录用。"""
        from sqlmodel import select

        return select(Skill).order_by(Skill.name)


# ============ AuditLog ============


class AuditLog(SQLModel, table=True):
    """审计日志（append-only）。数据面交互 + 控制面管理操作都落一条。

    数据面：记录谁、何时、问了什么、被放行/拒绝了什么、LLM 回了什么。
    控制面：记录操作者、动作（create_user/update_role 等）、变更前后值。
    合规与事后排查的命脉。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, index=True)
    user_name: str = Field(default="", index=True)
    channel: str = Field(default="api")  # im/api/admin
    action: str = Field(index=True)  # chat / create_user / update_role / delete_kb ...
    question: str = ""  # 数据面：用户问题；控制面：操作目标实体
    decision: str = ""  # allowed / denied / error（数据面用）
    detail: str = ""  # 拒绝原因 / 工具名 / 检索命中等
    answer: str = ""  # 数据面：LLM 回复；控制面：未用
    # 控制面变更审计：变更前/后的 JSON 快照（敏感字段已脱敏）。
    before: str = ""
    after: str = ""
    # 数据面工具调用审计：``[{name, args, result}]`` JSON 字符串。
    # result 是脱敏**后**的工具返回值（与进 LLM 上下文的一致，已截断防爆），
    # 对应 product-design 支柱 3「调用后审计落库」+ 支柱 4 第二条泄漏面。
    tool_results: str = ""
    created_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, index=True),
    )

    @staticmethod
    def select_recent(limit: int = 100):
        from sqlmodel import select

        return select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)


# ============ Model：模型实体（参考 WeKnora）============


class Model(SQLModel, table=True):
    """模型实体——LLM/向量模型是一等公民的可管理资源，不是配置项。

    每种 ``model_type``（chat/embedding/rerank）可有多个 Model 行，其中
    ``is_active=True`` 的是当前使用的（同类型只能一个 active）。
    admin 在控制台 CRUD + 激活 + 连通性测试。

    与 SystemSetting 的区别：SystemSetting 是扁平 key-value（IM 凭证等），
    Model 是结构化实体（多模型共存 + 默认 + 测试）。
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # 模型名（如 glm-4-flash、text-embedding-3-small）
    display_name: str = ""  # 显示名（"智谱 GLM-4-Flash"）
    model_type: str = Field(index=True)  # "chat" / "embedding" / "rerank"
    source: str = "remote"  # "remote" / "local"（local 预留 Ollama）
    provider: str = "generic"  # provider 标识（见 llm/providers.py）
    base_url: str = ""  # API 地址（remote 必填）
    api_key: str = ""  # 密钥（enc:v1 Fernet 密文；API 脱敏返回）
    # 仅 embedding 类型用：向量维度（如 1536/1024/768）。
    # 用于向量库 collection schema + 切换模型时的维度不匹配检测。
    embed_dimension: int = 0
    is_active: bool = Field(default=False, index=True)  # 每种 model_type 只有一个 active
    description: str = ""
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_active_by_type(model_type: str):
        """查某类型的当前激活模型。"""
        from sqlmodel import select

        return select(Model).where(
            Model.model_type == model_type,
            Model.is_active == True,  # noqa: E712
        )

    @staticmethod
    def select_by_type(model_type: str):
        """列某类型的全部模型。"""
        from sqlmodel import select

        return select(Model).where(Model.model_type == model_type).order_by(Model.id)


# ============ MCPServer：MCP 工具服务器（M4 工具网关）============


class MCPServer(SQLModel, table=True):
    """MCP 工具服务器——admin 注册的外部工具源。

    一个 MCPServer = 一个 MCP server 连接（stdio 本地子进程 或 streamable HTTP 远程）。
    启动/热重载时，``mcp/manager.MCPManager`` 对每个 ``is_enabled=True`` 的 server 建持久
    连接，发现其工具并注册进 ``tools/registry._REGISTRY``（name = ``mcp__{name}__{tool}``），
    对治理管道透明——RBAC 靠既有 ``RoleConfig.allowed_tools`` 白名单原样过滤。

    凭证（红线 #8）：``env_encrypted`` / ``headers_encrypted`` 用 Fernet 加密整段 JSON，
    API 只回脱敏值（与 ``Model.api_key`` 同处理）。明文绝不进日志。
    """

    __table_args__ = (UniqueConstraint("name", name="uq_mcpserver_name"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # 唯一；工具名前缀 mcp__{name}__
    description: str = ""
    transport: str = "stdio"  # "stdio" / "http"
    # stdio 传输：本地子进程。
    command: str = ""  # 可执行命令（如 uvx / npx / python）
    args_json: str = "[]"  # JSON 编码的 list[str]，命令参数
    env_encrypted: str = ""  # Fernet 密文（JSON dict）；空 = 无环境变量
    # http 传输：streamable HTTP 远程 server。
    url: str = ""  # MCP endpoint（如 https://host/mcp）
    headers_encrypted: str = ""  # Fernet 密文（JSON dict）；空 = 无自定义 header
    is_enabled: bool = Field(default=True)  # 禁用 → 其工具不注册、不可见
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_all():
        """列全部 MCP server（按 id 升序）。"""
        from sqlmodel import select

        return select(MCPServer).order_by(MCPServer.id)

    @staticmethod
    def select_enabled():
        """列已启用的 MCP server（启动时注册用）。"""
        from sqlmodel import select

        return select(MCPServer).where(MCPServer.is_enabled == True)  # noqa: E712

    @staticmethod
    def select_by_name(name: str):
        """按 name 查。"""
        from sqlmodel import select

        return select(MCPServer).where(MCPServer.name == name)


# ============ ApprovalRequest：HITL 审批队列（M4 Slice C）============


class ApprovalRequest(SQLModel, table=True):
    """破坏性工具调用的人审请求 M4 HITL 完整闭环。

    当 LLM 调用一个标记了 destructiveHint（或显式 requires_approval）的工具时，
    工具网关的审批门拦截执行：不直接调用工具，而是创建一条 pending 审批请求，
    返回待审批消息给 LLM（LLM 据此告知用户已提交审批）。admin 在审批队列里
    review 后 approve（执行工具 + 记录结果）或 reject。

    闭环（红线 #5 补完 MCP 工具的破坏性操作走人审）：
    审批通过后用存储的参数实际执行工具（重新计算能力集验证权限仍在），结果脱敏后
    存 result_text 并追加到会话历史（用户下次交互可见），审计落库。

    设计要点：tool_args_json 存 LLM 生成的原始参数 JSON 审批通过后原样回放执行；
    status 状态机 pending, executed, rejected, failed 不可逆；conversation_id 可空
    （REST 单轮无会话时仅记录不回写）；执行时重新计算能力集 若权限已变更则拒绝执行。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    user_name: str = ""
    conversation_id: int | None = Field(default=None, index=True)
    channel: str = ""
    im_user_id: str = ""  # M6：发起请求时的 IM userid（审批通过后主动推送结果用）
    tool_name: str = Field(index=True)
    tool_args_json: str = "{}"
    status: str = Field(default="pending", index=True)
    reviewed_by: str = ""
    reviewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime))
    review_note: str = ""
    result_text: str = ""
    executed_at: datetime | None = Field(default=None, sa_column=Column(DateTime))
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_pending():
        """列待审批请求（按创建时间倒序，最新的在前）。"""
        from sqlmodel import select

        return (
            select(ApprovalRequest)
            .where(ApprovalRequest.status == "pending")
            .order_by(ApprovalRequest.created_at.desc())
        )

    @staticmethod
    def select_by_status(status: str):
        """按状态列审批请求（按创建时间倒序）。"""
        from sqlmodel import select

        return (
            select(ApprovalRequest)
            .where(ApprovalRequest.status == status)
            .order_by(ApprovalRequest.created_at.desc())
        )

    @staticmethod
    def select_all():
        """列全部审批请求（按创建时间倒序）。"""
        from sqlmodel import select

        return select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())


# ============ KnowledgeBase：知识库实体（参考 WeKnora，per-KB embedding + collection）============


class KnowledgeBase(SQLModel, table=True):
    """知识库——一组文档的物理容器，绑定一个 embedding 模型 + 一个 zvec collection。

    参考腾讯 WeKnora 的 KnowledgeBase 实体设计：每个 KB 有独立的 embedding 模型
    （决定向量维度）、独立的 zvec collection（维度在创建时固定）、独立的切块统计。

    **KB 和 source(ACL) 分离**（对齐 WeKnora）：
    - KB 管物理存储层：embedding 模型 / 维度 / collection。
    - source 管逻辑访问层：ACL 标签（对应角色 knowledge_scope_tags）。
    - 一个 KB 可包含多种 source 标签的文档；一种 source 标签也可跨多个 KB。
    - ``visible_to_roles``：KB 级角色可见性（空 = 全部角色可见）。与 source ACL
      正交：source ACL 决定"文档内哪些 source 标签可被检索"，visible_to_roles 决定
      "哪个角色能在 admin 控制面看到/管理这个 KB"。

    这让管理员可以按"知识域 + 维度需求"灵活组合：例如用 1024 维的 bge-m3
    建"产品知识库"，同时用 768 维的 bce-embedding 建"HR 知识库"，各自独立。
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # KB 名称（如 "产品知识库"）
    description: str = ""
    embedding_model_id: int = Field(index=True)  # 绑定的 embedding 模型（Model.id）
    dimension: int = 0  # 向量维度（冗余存，建 collection 时用，从 Model.embed_dimension 读）
    collection_name: str = Field(index=True)  # zvec collection 名（确定性: f"kb_{id}"）
    visible_to_roles: list[str] | None = Field(
        default=None, sa_column=Column(ListJSONType)
    )  # KB 级角色可见性（空=全部角色可见）

    # ---- 切块配置（per-KB，参考 WeKnora ChunkingConfig）----
    chunk_size: int = 500  # 默认单块字符数（非父子模式）
    chunk_overlap: int = 50  # 重叠字符数
    separators: list[str] = Field(
        default_factory=lambda: [
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            "；",
            ".",
            "!",
            "?",
            ";",
            "，",
            ",",
            " ",
        ],
        sa_column=Column(ListJSONType),
    )  # 递归切分分隔符（优先级从高到低）
    strategy: str = "auto"  # 切块策略: "auto" | "heading" | "recursive"
    enable_parent_child: bool = False  # 是否开启父子块切分
    parent_chunk_size: int = 4096  # 父块字符数（父子模式）
    child_chunk_size: int = 384  # 子块字符数（父子模式）
    # ---- 解析引擎映射（per-KB，WeKnora 风格：每种文件类型可指定不同引擎）----
    # {文件扩展名: 引擎名}，如 {"pdf": "mineru", "docx": "markitdown", "default": "builtin"}。
    # 解析时优先级：上传显式 engine > map[ext] > map["default"] > 全局 settings.parse.engine。
    # 空 / None → 全部用全局默认引擎。
    parse_engine_map: dict[str, str] | None = Field(
        default=None, sa_column=Column("parse_engine_map", DictJSONType)
    )
    # ---- 统计 ----
    total_chars: int = 0  # 文档总字符数
    document_count: int = 0  # 文档数（冗余计数）
    chunk_count: int = 0  # 切块总数（冗余计数）
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_name(name: str):
        """按 name 查（唯一）。"""
        from sqlmodel import select

        return select(KnowledgeBase).where(KnowledgeBase.name == name)

    @staticmethod
    def select_all():
        """全部 KB，按 id 升序。"""
        from sqlmodel import select

        return select(KnowledgeBase).order_by(KnowledgeBase.id)


# ============ Document：知识库文档（参考 WeKnora Knowledge 实体）============


class Document(SQLModel, table=True):
    """知识库文档——入库管理的一等公民。

    参考腾讯 WeKnora 的 Knowledge 实体设计：文档有明确的生命周期状态
    （pending → processing → completed/failed），记录入库时用的 embedding 模型
    （维度锁定），支持级联删除与重新入库（reindex）。

    - ``source``：知识域 ACL 标签（如 "public"/"sales"/"hr"），对应角色的
      ``knowledge_scope_tags``。检索时翻译成 ``source IN (...)`` 预过滤——
      这条把治理脊柱的 ACL 预过滤与知识库管理打通。
    - ``embedding_model_id``：入库时用的 embedding 模型 id（维度锁定）。
      切换 embedding 模型后维度可能不匹配，需 reindex 才能重新检索。
    - ``status``：``pending``（已创建未入库）→ ``processing``（入库中）→
      ``completed``（成功）/ ``failed``（失败，``error_message`` 记原因）。

    M1 不存物理文件路径——文件上传读取文本后即入库，不落盘（WeKnora 有下载/预览
    需求才存 FilePath，Bycall M1 不做）。
    """

    id: int | None = Field(default=None, primary_key=True)
    knowledge_base_id: int = Field(index=True)  # 所属 KB（决定 collection + embedding）
    title: str = Field(index=True)  # 文档标题
    source: str = Field(index=True)  # 知识域 ACL 标签（对应 Role.knowledge_scope_tags）
    content: str = ""  # 原始正文（manual/file 解析后文本）
    status: str = Field(default="pending", index=True)  # pending|processing|completed|failed
    chunk_count: int = 0  # 切块数（入库成功后更新）
    char_count: int = 0  # 正文字符数
    error_message: str = ""  # 失败原因（status=failed 时填）
    file_name: str = ""  # 上传文件名（file 模式；manual 为空）
    file_type: str = ""  # txt/md/markdown
    embedding_model_id: int | None = Field(default=None)  # 入库时用的 embedding 模型（维度锁定）
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_source(source: str):
        """按知识域标签查（列表过滤用）。"""
        from sqlmodel import select

        return select(Document).where(Document.source == source).order_by(Document.id.desc())

    @staticmethod
    def select_by_kb(knowledge_base_id: int):
        """按所属 KB 查（列表过滤用）。"""
        from sqlmodel import select

        return (
            select(Document)
            .where(Document.knowledge_base_id == knowledge_base_id)
            .order_by(Document.id.desc())
        )

    @staticmethod
    def select_all():
        """全部文档，按 id 倒序（最新在前）。"""
        from sqlmodel import select

        return select(Document).order_by(Document.id.desc())

    @staticmethod
    def select_distinct_sources():
        """所有已用过的 source 标签（供前端下拉 + 角色配置参考）。"""
        from sqlmodel import distinct, select

        return select(distinct(Document.source))


# ============ Conversation + Message：会话状态层（参考 Hermes SessionStore）============


class Conversation(SQLModel, table=True):
    """会话——一个 (user_id, channel) 对应一条，永不过期。

    参考 Hermes Agent 的 SessionEntry：会话有确定性 session_key、
    可选 model_id（per-session 模型，空→fallback 全局激活）、summary（压缩摘要）。

    会话生命周期：每渠道独立 + 永不过期（用户确认）。一个 (user_id, channel)
    永远对应一条 Conversation，上下文按会话隔离，提示词/能力集/默认模型全局共享。
    消息全量存 DB（审计需要），压缩只影响拼 prompt 时取多少条（summary + recent N）。

    与 Hermes 的差异：Hermes 压缩后删中间消息创建 lineage 子会话；
    Bycall 保留全量消息，summary 原地更新。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)  # User.id
    channel: str = Field(index=True)  # "api" / "wecom" / "feishu"
    session_key: str = Field(unique=True, index=True)  # 确定性: f"{channel}:{user_id}"
    model_id: int | None = Field(default=None)  # per-session 模型（空→全局激活）
    summary: str = ""  # 压缩摘要（增量更新，非从头重写）
    message_count: int = 0  # 消息总数
    last_message_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_session_key(session_key: str):
        """按 session_key 查（确定性查找）。"""
        from sqlmodel import select

        return select(Conversation).where(Conversation.session_key == session_key)

    @staticmethod
    def select_by_user(user_id: int):
        """按 user_id 查（列表过滤）。"""
        from sqlmodel import select

        return (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.id.desc())
        )

    @staticmethod
    def select_all():
        """全部会话，按最后消息时间倒序。"""
        from sqlmodel import select

        return select(Conversation).order_by(Conversation.last_message_at.desc())


class Message(SQLModel, table=True):
    """单条消息——role + content，按 conversation_id 关联。

    参考 Hermes 的 messages 表（role/content/token_count）。
    user message 和 assistant answer 分两条存（对齐 Dify query+answer 模式，
    便于审计单独查 user 问题或 assistant 回答）。
    """

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True)  # Conversation.id
    role: str = Field(index=True)  # "user" / "assistant" / "system"
    content: str  # 消息正文
    token_count: int = 0  # token 计数（压缩判断用；粗估 = len(content)//4）
    created_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, index=True),
    )

    @staticmethod
    def select_recent(conversation_id: int, limit: int = 20):
        """取最近 N 条消息（按 created_at 倒序取，再正序返回用于拼 prompt）。"""
        from sqlmodel import select

        return (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )

    @staticmethod
    def select_by_conversation(conversation_id: int):
        """全部消息（按时间正序，分页查历史用）。"""
        from sqlmodel import select

        return (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )


class UserMemory(SQLModel, table=True):
    """用户级长期记忆——跨会话持久，admin 可见可编辑。

    参考 Hermes 的 USER.md + MEMORY.md，但关键差异：Bycall 的记忆不由 LLM 自主编辑
    （企业场景：记忆是组织管控的，不是 LLM 自己写的）。M3 先做表结构 + admin 手动管理，
    LLM 自动提取记忆留后续迭代。

    记忆在拼 prompt 时注入 system message（与 system_prompt 一起，不是 user message）。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)  # User.id
    category: str = Field(index=True)  # "preference" / "fact" / "note"
    content: str  # 记忆内容
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_user(user_id: int):
        """按 user_id 查全部记忆。"""
        from sqlmodel import select

        return select(UserMemory).where(UserMemory.user_id == user_id).order_by(UserMemory.id)


# ============ SystemSetting：可编辑运行时配置 ============


class SystemSetting(SQLModel, table=True):
    """可编辑运行时配置（key-value）。admin 在控制台管理，存 DB 而非 .env。

    分层约定（避免鸡生蛋）：
    - ``.env`` 只留启动引导：DB DSN / session_secret / env / debug / log_level
      （这些在"连上 DB 之前"就需要）
    - 本表管运行时可编辑的：LLM 全部配置（provider/base_url/api_key/model/embed_model）、
      IM 全部配置（enable/bot_id/secret）、解析引擎配置（parse.engine / parse.mineru_* /
      parse.markitdown_enable_plugins）

    优先级：DB 值 > .env 环境变量 > 代码默认。DB 有值即时生效，无需重启。
    ``is_secret=True`` 的 key 在 API/UI 返回时脱敏（不回显明文）。

    key 命名：扁平，如 ``llm.provider`` / ``llm.api_key`` / ``im.enable`` / ``im.wecom_bot_id``
    / ``parse.engine`` / ``parse.mineru_base_url``。
    """

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)  # 如 "llm.api_key"
    value: str = ""  # 字符串存储；非字符串值由调用方做类型转换
    is_secret: bool = Field(default=False)  # True → API/UI 脱敏（不回显）
    description: str = ""
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_key(key: str):
        from sqlmodel import select

        return select(SystemSetting).where(SystemSetting.key == key)

    @staticmethod
    def select_all():
        from sqlmodel import select

        return select(SystemSetting).order_by(SystemSetting.key)


# ============ Task/Step/Run：持久化多步任务（S1a 执行核心）============


class Task(SQLModel, table=True):
    """持久化多步任务——用户目标拆成 Step 序列逐步执行（含 HITL 暂停）。

    run_agent 的工具回路（run_tool_loop）成为 **Step 的执行内核**（不重写）：Task 在外层
    编排状态机，每步执行前重算能力集（复用 execute_approved_tool 的已验证模式）。状态显式
    持久化，paused（等审批/等输入）可跨进程恢复。详见 ADR-0005 + spec agent-framework-upgrade。

    与 Conversation 的关系（S1a）：1:1，Task 关联当前 conversation_id；跨会话续跑留后置。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)  # User.id
    conversation_id: int | None = Field(default=None, index=True)  # Conversation.id（S1a 1:1）
    channel: str = ""  # im / api（结果推送 + 审计归因）
    trigger: str = Field(
        default="conversation", index=True
    )  # conversation / cron / event（来源入口）
    schedule_id: int | None = Field(
        default=None, index=True
    )  # 关联 TaskSchedule（cron/event 触发时填）
    goal: str  # 用户目标（自然语言）
    plan_json: str = "[]"  # LLM 产出的 Step 序列（建议性，执行前代码层校验）
    status: str = Field(
        default="draft", index=True
    )  # draft/planning/running/paused/completed/failed/cancelled
    capability_snapshot_json: str = "{}"  # 创建时能力集快照（每步重算后更新）
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_user(user_id: int):
        """按 user_id 列任务（创建时间倒序，最新在前）。"""
        from sqlmodel import select

        return select(Task).where(Task.user_id == user_id).order_by(Task.created_at.desc())

    @staticmethod
    def select_by_status(status: str):
        """按状态列任务（创建时间倒序）。"""
        from sqlmodel import select

        return select(Task).where(Task.status == status).order_by(Task.created_at.desc())

    @staticmethod
    def select_by_conversation(conversation_id: int):
        """按会话查任务（S1a 1:1，取最新一条）。"""
        from sqlmodel import select

        return (
            select(Task)
            .where(Task.conversation_id == conversation_id)
            .order_by(Task.created_at.desc())
        )


class Step(SQLModel, table=True):
    """Task 的单步——状态机的最小执行单元。

    kind 决定执行方式：
    - ``tool_loop`` → 调 run_tool_loop（工具回路内核，复用 graph.py，不重写）
    - ``approval_wait`` → 等审批（转 ApprovalRequest）
    - ``clarify`` → 等用户澄清（pause 后由下一条消息恢复）
    - ``deterministic`` → 确定性代码（如发推送，无 LLM）

    ``spec_json`` 存该步参数（工具名/确定性行为/审批要求）。``idempotency_key`` 防恢复时
    重复执行（同 key 的 done Step 不重跑）。
    """

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(index=True)  # Task.id
    step_index: int  # 序号（0-based，执行顺序）
    kind: str = Field(
        default="tool_loop", index=True
    )  # tool_loop/deterministic/approval_wait/clarify
    spec_json: str = "{}"  # 该步参数（工具名/确定性行为/审批要求）
    idempotency_key: str = Field(default="", index=True)  # 幂等防重复
    status: str = Field(default="pending", index=True)  # pending/running/done/failed/skipped
    retry_count: int = 0
    max_retries: int = 0
    created_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    updated_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, onupdate=_now_utc),
    )

    @staticmethod
    def select_by_task(task_id: int):
        """按 task_id 列全部 Step（step_index 正序，执行顺序）。"""
        from sqlmodel import select

        return select(Step).where(Step.task_id == task_id).order_by(Step.step_index)


class Run(SQLModel, table=True):
    """Step 的一次执行尝试——Step 可重试，每次尝试落一条 Run。

    最后一次成功的 Run 决定 Step 的产出。``tool_calls_json`` 复用
    ``GraphResult.tool_calls_made``（脱敏后的工具调用记录），与审计字段对齐。
    """

    id: int | None = Field(default=None, primary_key=True)
    step_id: int = Field(index=True)  # Step.id
    input_json: str = "{}"  # 执行输入快照
    output_json: str = "{}"  # 执行输出快照
    started_at: datetime = Field(
        default_factory=_now_utc, sa_column=Column(DateTime, default=_now_utc)
    )
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime))
    duration_ms: int = 0  # 耗时（finished - started，毫秒）
    status: str = Field(default="ok", index=True)  # ok / error / timeout
    tool_calls_json: str = "[]"  # 该次尝试的工具调用（脱敏，复用 GraphResult）

    @staticmethod
    def select_by_step(step_id: int):
        """按 step_id 列全部 Run（started_at 正序）。"""
        from sqlmodel import select

        return select(Run).where(Run.step_id == step_id).order_by(Run.started_at)
