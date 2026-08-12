"""Admin REST API 的请求/响应 schema（pydantic，不落表）。

这些是控制面 API 的传输契约，与 ``governance.models`` 里的 ORM 表模型分离：
- 表模型 = 持久化真值（带 ``table=True``）
- 这里的 schema = 对外的字段筛选/转换层（例如 ``UserOut`` 绝不暴露 ``password_hash``）

设计约定：
- 复用 ``RoleConfig``（治理领域模型里的嵌套对象），避免在 schema 层重复定义权限结构。
- 响应 schema（``*Out``）开 ``from_attributes=True``，可直接 ``Model.model_validate(orm_obj)``
  从 SQLModel 行构造。
- 更新 schema 的字段一律 Optional，PATCH 语义 = 只改传入字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bycall.governance.models import RoleConfig

# ============ 用户 ============


class UserCreate(BaseModel):
    """创建用户请求。

    ``password`` 可选——不传时由 admin 在控制面后续设置（或 IM 用户根本不需要密码）。
    ``im_bindings`` 可选——绑定时校验 (channel, im_user_id) 全局唯一（防误绑到他人）。
    """

    name: str = Field(..., description="登录名/业务标识，全局唯一")
    display_name: str = Field(default="", description="显示名")
    role: str = Field(default="member", description="角色名（Role.name 或内置 admin）")
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="明文密码，不传则留空（IM 用户无需密码）",
    )
    is_active: bool = Field(default=True, description="是否启用")
    im_bindings: dict[str, str] | None = Field(
        default=None, description="IM 绑定 {channel: im_user_id}，校验全局唯一"
    )


class UserUpdate(BaseModel):
    """更新用户请求（PATCH 语义：仅传入字段生效）。

    传 ``password`` 表示改密码；传 ``role`` 会校验目标角色存在。
    传 ``im_bindings`` 更新 IM 绑定（校验全局唯一）。
    """

    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(
        default=None, min_length=8, max_length=128, description="传非空值即改密码"
    )
    im_bindings: dict[str, str] | None = Field(
        default=None, description="更新 IM 绑定，校验全局唯一"
    )


class UserOut(BaseModel):
    """用户响应。绝不包含 ``password_hash``。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    role: str
    is_active: bool
    im_bindings: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


# ============ 角色 ============


class RoleCreate(BaseModel):
    """创建角色请求。``name`` 不能是保留字 ``admin``（见 api 层校验）。"""

    name: str = Field(..., description="角色名，主键")
    description: str = Field(default="", description="人类可读说明")
    config: RoleConfig = Field(default_factory=RoleConfig, description="权限配置")


class RoleUpdate(BaseModel):
    """更新角色请求（PATCH 语义）。"""

    description: str | None = None
    config: RoleConfig | None = None


class RoleOut(BaseModel):
    """角色响应。"""

    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    config: RoleConfig
    created_at: datetime


# ============ 系统提示词 ============


class PromptCreate(BaseModel):
    """创建新版本提示词。``version`` 全局唯一、递增。"""

    version: int = Field(..., description="版本号，全局唯一")
    label: str = Field(default="", description="人类可读标签")
    content: str = Field(..., description="提示词正文")
    is_active: bool = Field(
        default=False,
        description="创建时是否直接激活（默认 False，建议用 /activate 端点原子切换）",
    )


class PromptOut(BaseModel):
    """提示词响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    label: str
    content: str
    is_active: bool
    created_at: datetime


class PromptActivate(BaseModel):
    """激活某版本提示词的请求体。

    服务端会先把所有版本 ``is_active`` 置 False，再把目标版本置 True，事务内原子完成。
    """

    version: int = Field(..., description="要激活的版本号")


class PromptUpdate(BaseModel):
    """更新提示词（PATCH 语义，只改传入字段）。"""

    version: int | None = None
    label: str | None = None
    content: str | None = None


# ============ Skill（领域知识文档，对齐 OpenClaw SKILL）============


class SkillCreate(BaseModel):
    """创建 Skill。``name`` 全局唯一（如 ``sales-playbook``）。"""

    name: str = Field(
        ...,
        # 小写字母/数字/连字符，首字符非连字符；admin 可控故只做格式约束（非安全门）。
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="唯一标识，小写字母/数字/连字符，首字符非连字符，如 sales-playbook",
    )
    description: str = Field(default="", description="一句话描述（admin 目录展示，不进 prompt）")
    content: str = Field(..., description="Markdown body（领域知识，进 system prompt）")
    disclosure: str | None = Field(
        default=None,
        description="披露模式：always（始终注入 system prompt）| on_demand（按需加载，默认 always）",
    )


class SkillUpdate(BaseModel):
    """更新 Skill（PATCH 语义，只改传入字段）。可迭代文档，无需发新版本。"""

    description: str | None = None
    content: str | None = None
    is_enabled: bool | None = None
    disclosure: str | None = None


class SkillOut(BaseModel):
    """Skill 响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    content: str
    is_enabled: bool
    disclosure: str = "always"
    created_at: datetime
    updated_at: datetime


# ============ 审计 ============


class AuditOut(BaseModel):
    """审计日志响应。append-only，只读。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    user_name: str
    channel: str
    action: str
    question: str
    decision: str
    detail: str
    answer: str
    before: str = ""
    after: str = ""
    tool_results: str = ""
    # 本轮拼给 LLM 的上下文快照（JSON）：system 正文/RAG/记忆/summary/历史。
    # admin 排障用——重建「模型那一轮看到了什么」。各段已截断。
    context_trace: str = ""
    created_at: datetime


class AuditPage(BaseModel):
    """审计日志分页响应。"""

    items: list[AuditOut]
    total: int
    page: int
    page_size: int


# ============ 运行时设置（LLM/IM 凭证）============


class SettingOut(BaseModel):
    """设置项响应。``is_secret=True`` 时 value 返回脱敏值（绝不回显明文）。"""

    model_config = ConfigDict(from_attributes=True)

    key: str
    value: str  # secret 已脱敏
    is_secret: bool
    description: str
    is_set: bool  # 是否已配置（非空）


class SettingUpdate(BaseModel):
    """更新设置项。value 为空串表示清除该设置（回退到 .env/默认）。"""

    value: str = Field(..., description="新值；空串=清除")


class SettingsBatchUpdate(BaseModel):
    """批量更新设置（一个表单提交多个 key）。"""

    settings: dict[str, str] = Field(..., description="{key: value} 映射；value 空串=清除该 key")


# ============ Model（模型实体管理，参考 WeKnora）============


class ModelCreate(BaseModel):
    """创建模型请求。``name`` + ``model_type`` 联合唯一（同类型下重名 → 409）。"""

    name: str = Field(..., description="模型名，如 glm-4-flash")
    display_name: str = Field(default="", description="人类可读显示名")
    model_type: str = Field(..., description='模型类型："chat" / "embedding" / "rerank"')
    source: str = Field(default="remote", description='"remote" / "local"')
    provider: str = Field(default="generic", description="provider 标识（见 llm/providers.py）")
    base_url: str = Field(default="", description="API 地址（remote 必填）")
    api_key: str = Field(default="", description="密钥，加密落库、API 脱敏返回")
    embed_dimension: int = Field(default=0, description="向量维度（仅 embedding 用）")
    description: str = Field(default="", description="备注说明")
    is_active: bool = Field(default=False, description="创建时是否直接激活同类型其他置 False")


class ModelUpdate(BaseModel):
    """更新模型请求（PATCH 语义：仅传入字段生效）。

    ``api_key`` 若是含 ``***`` 的脱敏占位符（前端原样回填的展示值）视为"未改"，跳过。
    """

    display_name: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(default=None, description="含 *** 的脱敏占位符视为未改")
    embed_dimension: int | None = None
    description: str | None = None


class ModelOut(BaseModel):
    """模型响应。``api_key`` 只返回脱敏值 + 是否已设，绝不回显明文。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    model_type: str
    source: str
    provider: str
    base_url: str
    api_key_masked: str = Field(default="", description="脱敏后的 api_key")
    has_api_key: bool = Field(default=False, description="是否已设置 api_key")
    embed_dimension: int
    is_active: bool
    description: str
    created_at: datetime
    updated_at: datetime


class ModelTestResult(BaseModel):
    """连通性测试结果。失败时 ok=False + error；成功时附带响应片段。

    embedding 模型测试成功时附带实测维度（``embed_dimension``），并自动回填到实体落库。
    """

    ok: bool
    latency_ms: int = 0
    error: str = ""
    detail: str = Field(default="", description="成功时返回的样例响应片段")
    embed_dimension: int | None = Field(
        default=None,
        description="embedding 模型实测维度（仅 embedding；测试时自动探测并回填到实体）",
    )


# ============ KnowledgeBase（知识库实体，参考 WeKnora）============


class KnowledgeBaseCreate(BaseModel):
    """创建知识库请求。

    选一个 embedding 模型 → 自动读维度 → 建 zvec collection。
    KB 和 source(ACL) 分离：KB 管维度/embedding/collection，source 管 ACL。
    ``visible_to_roles`` 为 None/空 = 全部角色可见。
    """

    name: str = Field(..., description="知识库名称，全局唯一")
    description: str = Field(default="", description="描述")
    embedding_model_id: int = Field(..., description="绑定的 embedding 模型 ID（决定维度）")
    visible_to_roles: list[str] | None = Field(
        default=None, description="可见角色名列表（空=全部角色可见）"
    )
    # ---- 切块配置（沿用 WeKnora 默认值，可不传）----
    chunk_size: int = Field(default=500, ge=100, le=4000, description="单块字符数")
    chunk_overlap: int = Field(default=50, ge=0, le=500, description="重叠字符数")
    separators: list[str] | None = Field(
        default=None, description="递归切分分隔符（默认：换行-句号-逗号优先级链）"
    )
    strategy: str = Field(default="auto", description='切块策略: "auto" | "heading" | "recursive"')
    enable_parent_child: bool = Field(default=False, description="是否开启父子块切分")
    parent_chunk_size: int = Field(default=4096, ge=512, le=8192, description="父块字符数")
    child_chunk_size: int = Field(default=384, ge=64, le=2048, description="子块字符数")


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库（PATCH 语义）。只改名称/描述/可见角色列表，不改 embedding 模型（维度锁定）。"""

    name: str | None = None
    description: str | None = None
    visible_to_roles: list[str] | None = None
    # ---- 切块配置（调参不会触发已有文档重索引）----
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=500)
    separators: list[str] | None = None
    strategy: str | None = None
    enable_parent_child: bool | None = None
    parent_chunk_size: int | None = Field(default=None, ge=512, le=8192)
    child_chunk_size: int | None = Field(default=None, ge=64, le=2048)
    # per-KB 文件类型→解析引擎映射（WeKnora 风格）；None=不改，{}=清空（回退全局默认）。
    parse_engine_map: dict[str, str] | None = None


class KnowledgeBaseOut(BaseModel):
    """知识库响应。"""

    model_config = ConfigDict(from_attributes=True)
    # ---- 切块配置 ----
    chunk_size: int
    chunk_overlap: int
    separators: list[str]
    strategy: str
    enable_parent_child: bool
    parent_chunk_size: int
    child_chunk_size: int
    parse_engine_map: dict[str, str] | None = None
    total_chars: int

    id: int
    name: str
    description: str
    embedding_model_id: int
    dimension: int
    collection_name: str
    visible_to_roles: list[str] | None = None
    document_count: int
    chunk_count: int
    created_at: datetime
    updated_at: datetime


# ============ Document（知识库文档管理，参考 WeKnora Knowledge 实体）============


class DocumentCreate(BaseModel):
    """创建文档并入库请求。

    ``source`` 是知识域 ACL 标签（如 "public"/"sales"/"hr"），对应角色的
    ``knowledge_scope_tags``——只有该标签在角色配置里的用户才能检索到本文档。
    """

    title: str = Field(..., description="文档标题")
    source: str = Field(
        ..., description="知识域标签，如 public/sales/hr（对应角色 knowledge_scope_tags）"
    )
    content: str = Field(..., description="文档正文（手动输入或文件解析后文本）")
    file_name: str = Field(default="", description="来源文件名（可选，用于显示）")
    knowledge_base_id: int = Field(..., description="所属知识库 ID")


class DocumentUpdate(BaseModel):
    """更新文档请求（PATCH 语义）。

    改 ``source`` 会触发重新入库（chunk 的 source 标签需同步更新）。
    """

    title: str | None = None
    source: str | None = Field(default=None, description="改 source 会触发重新入库")


class DocumentOut(BaseModel):
    """文档响应。不返回 ``content``（可能很长，列表/详情均只看元数据）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    knowledge_base_id: int
    title: str
    source: str
    status: str
    chunk_count: int
    char_count: int
    error_message: str
    file_name: str
    file_type: str
    embedding_model_id: int | None = None
    created_at: datetime
    updated_at: datetime


class DocumentIngestResult(BaseModel):
    """入库操作结果。"""

    ok: bool
    document_id: int
    chunk_count: int
    errors: list[str] = Field(default_factory=list)


class IngestAccepted(BaseModel):
    """入库任务已受理（异步）：文档已建/已定位，入库在后台进行。

    前端拿到后轮询 ``GET .../documents/{id}/progress`` 直到 status 终态。
    """

    document_id: int
    status: str = "processing"


class IngestProgressOut(BaseModel):
    """入库进度响应：DB 持久态（status/chunk_count/error_message/title）+
    内存态实时阶段（stage/percent/total_chunks/processed_chunks）合并。

    - ``status``：DB 的终态/持久态 pending|processing|completed|failed（权威）。
    - ``stage``：内存 tracker 的实时阶段 "" | chunking | embedding | indexing | done | failed。
    - ``chunk_count``：DB 已落库的子块数（入库完成后才有值）。
    """

    document_id: int
    title: str
    status: str
    stage: str
    percent: int
    total_chunks: int
    processed_chunks: int
    chunk_count: int
    error_message: str


# ============ Chunk 预览（参考 WeKnora chunker/preview，不落库不入库）============


class ChunkPreviewRequest(BaseModel):
    """切块预览请求——仅运行切块逻辑，不做 embedding、不写库。

    用于管理员在入库前/后调参验证切块效果（对齐 WeKnora 的 ``POST /chunker/preview``）。
    配置项留空则用入库时同样的默认 ``ChunkingConfig``，保证预览 == 实际入库效果。
    """

    text: str = Field(..., description="待切块的样例正文")
    title: str = Field(default="", description="文档标题（影响 heading 上下文，可选）")
    chunk_size: int | None = Field(default=None, description="单块字符数（默认 500）")
    overlap: int | None = Field(default=None, description="重叠字符数（默认 50）")
    strategy: str | None = Field(
        default=None, description='切块策略覆盖: "auto" | "heading" | "recursive"'
    )
    enable_parent_child: bool | None = Field(
        default=None, description="是否启用父子块切分（child=384 / parent=4096）"
    )


class ChunkPreviewItem(BaseModel):
    """单条切块预览。"""

    id: str = Field(
        default="", description="切块 id（父子模式下作为层级锚点，前端据此建立 parent→child 树）"
    )
    chunk_index: int
    chunk_type: str = Field(default="child", description="child | parent")
    char_count: int
    text: str
    parent_id: str | None = Field(
        default=None, description="父块 id：child 指向其 parent，parent 自身为 None"
    )


class ChunkPreviewResult(BaseModel):
    """切块预览结果。"""

    total: int
    child_count: int
    parent_count: int
    items: list[ChunkPreviewItem]


# ============ 文档正文（调试/核对入库内容用）============


class DocumentContentOut(BaseModel):
    """文档原始正文响应——DocumentOut 刻意不带 content（可能很长），需单独查。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    char_count: int
    content: str


# ============ Chunk 列表（调试/排查检索效果用）============


class KBChunkOut(BaseModel):
    """单个 chunk 的元数据（不含向量，仅标量字段 + 文本预览）。"""

    chunk_id: str
    chunk_type: str = Field(default="child", description="child | parent")
    chunk_index: int
    doc_id: str
    source: str
    title: str
    text: str = Field(default="", description="截断到前 500 字符的文本预览")
    char_count: int
    parent_id: str | None = None


class KBChunkListResult(BaseModel):
    """KB 内 chunk 列表（按 doc_id 过滤）。"""

    kb_id: int
    doc_id: str | None = None
    total: int
    items: list[KBChunkOut]


# ============ KB 检索（admin 调试用，不计入数据面链路）============


class KBSearchRequest(BaseModel):
    """在指定 KB 内执行混合检索（向量+FTS+权限过滤），用于 admin 验证检索效果。"""

    question: str = Field(..., description="检索查询文本")
    topk: int = Field(default=5, ge=1, le=50, description="返回条数")
    knowledge_scope_tags: list[str] | None = Field(
        default=None, description="模拟角色的知识域标签（ACL 预过滤）；None/空 = 不限"
    )


class KBSearchResultItem(BaseModel):
    """单条检索结果。"""

    chunk_id: str
    score: float
    text: str = Field(default="", description="截断到前 300 字符")
    chunk_type: str
    doc_id: str
    source: str
    title: str


class KBSearchResult(BaseModel):
    """KB 检索响应。"""

    kb_id: int
    question: str
    results: list[KBSearchResultItem]
    total: int


# ============ Batch 批量操作 ============


class BatchReindexRequest(BaseModel):
    """批量重新入库请求。"""

    document_ids: list[int] = Field(
        ..., min_length=1, max_length=100, description="要重新入库的文档 ID 列表"
    )


# ============ Conversation + Message（会话管理）============


class ConversationOut(BaseModel):
    """会话响应。不包含 summary（太长，按需单独查）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    channel: str
    session_key: str
    model_id: int | None = None
    message_count: int
    last_message_at: datetime
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    """单条消息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    token_count: int
    created_at: datetime


# ============ UserMemory（长期记忆管理）============


class MemoryCreate(BaseModel):
    """添加用户记忆条目。"""

    category: str = Field(..., description="分类：preference / fact / note")
    content: str = Field(..., description="记忆内容")


class MemoryUpdate(BaseModel):
    """更新记忆条目（PATCH 语义）。"""

    category: str | None = None
    content: str | None = None


class MemoryOut(BaseModel):
    """记忆条目响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    category: str
    content: str
    created_at: datetime
    updated_at: datetime


# ============ Tool（工具目录，M3 工具治理）============


class ToolOut(BaseModel):
    """工具目录条目（只读）。

    工具不是 DB 实体（实现是代码，见 ``tools/registry.py``），本 schema 仅用于
    控制面 ``GET /api/admin/tools`` 列出已注册工具目录，供 admin 在角色编辑时
    渲染 ``allowed_tools`` 多选框。``parameters`` 是 JSON Schema（OpenAI function calling 惯例）。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    source: str = "builtin"  # "builtin" / "mcp"——供前端按来源分组展示


# ============ MCPServer（M4 工具网关）============


class MCPServerCreate(BaseModel):
    """注册 MCP server。

    - ``transport="stdio"``：本地子进程，填 ``command``/``args``/``env``。
    - ``transport="http"``：streamable HTTP 远程，填 ``url``/``headers``。
    - ``env``/``headers`` 整段加密存库，绝不回显（红线 #8）。
    """

    name: str = Field(..., description="唯一名；工具前缀 mcp__{name}__")
    description: str = ""
    transport: str = Field(default="stdio", description='"stdio" / "http"')
    # stdio
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, description="环境变量，加密存库")
    # http
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict, description="自定义 header，加密存库")
    is_enabled: bool = True


class MCPServerUpdate(BaseModel):
    """更新 MCP server（PATCH 语义：仅传入字段生效）。

    ``env``/``headers`` 为 write-only：不传（None）= 不改；传 ``{}`` = 清空。
    """

    description: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    is_enabled: bool | None = None


class MCPServerOut(BaseModel):
    """MCP server 响应。env/headers 绝不回显明文，只标是否已配置。"""

    id: int
    name: str
    description: str
    transport: str
    command: str
    args: list[str]
    url: str
    is_enabled: bool
    has_env: bool  # 是否已配置 env（不回显值）
    has_headers: bool  # 是否已配置 headers（不回显值）
    tool_count: int = 0  # 已发现并注册的工具数（manager 提供）
    connected: bool = False  # 当前是否已连接
    created_at: datetime
    updated_at: datetime


class MCPTestResult(BaseModel):
    """MCP server 连通性测试结果。"""

    ok: bool
    message: str = ""
    tool_count: int = 0
    tools: list[str] = Field(default_factory=list, description="发现的工具名（空间化后）")


# ============ ApprovalRequest（M4 Slice C HITL 审批队列）============


class ApprovalOut(BaseModel):
    """审批请求响应。result_text 在审批通过执行后才有值。"""

    id: int
    user_id: int
    user_name: str
    conversation_id: int | None = None
    channel: str = ""
    task_id: int | None = None  # S1b：关联任务（任务内审批闭环）
    step_id: int | None = None
    tool_name: str
    tool_args: dict = Field(default_factory=dict, description="LLM 生成的调用参数")
    status: str  # pending / executed / rejected / failed
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    review_note: str = ""
    result_text: str = ""
    executed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ApprovalReview(BaseModel):
    """审批操作请求体（approve / reject 共用）。"""

    note: str = Field(default="", description="审批备注（可选）")


# ============ Skill 导入导出 ============


class SkillImportItem(BaseModel):
    """单条导入结果。"""

    name: str
    status: str  # created / updated / skipped
    message: str = ""


class SkillImportError(BaseModel):
    """导入解析错误。"""

    entry: str  # zip entry path
    reason: str


class SkillImportResult(BaseModel):
    """导入结果汇总。"""

    items: list[SkillImportItem] = Field(default_factory=list)
    errors: list[SkillImportError] = Field(default_factory=list)
    total_created: int = 0
    total_updated: int = 0
    total_skipped: int = 0


# ============ Task / Step / Run（S1 任务执行）============


class TaskOut(BaseModel):
    """任务列表项（控制面：admin 审计/跟进任务执行）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    user_name: str = ""
    conversation_id: int | None = None
    channel: str = ""
    trigger: str  # conversation / cron / event
    schedule_id: int | None = None
    goal: str
    status: str  # draft/planning/running/paused/completed/failed/cancelled
    created_at: datetime
    updated_at: datetime


class TaskStepOut(BaseModel):
    """任务步骤（详情页进度展示）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    step_index: int
    kind: str  # tool_loop / deterministic / approval_wait / clarify
    spec_json: str = "{}"
    status: str  # pending/running/done/failed/skipped/awaiting_approval
    retry_count: int = 0
    max_retries: int = 0
    created_at: datetime
    updated_at: datetime


class TaskRunOut(BaseModel):
    """步骤执行尝试（含工具调用明细与输出）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int
    input_json: str = "{}"
    output_json: str = "{}"
    tool_calls_json: str = "[]"
    status: str  # ok / error / timeout
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int = 0


class TaskDetailOut(BaseModel):
    """任务详情：Task + 步骤 + 每次尝试。"""

    task: TaskOut
    steps: list[TaskStepOut] = Field(default_factory=list)
    runs: list[TaskRunOut] = Field(default_factory=list)


# ============ TaskSchedule（S1b 定时/事件触发）============


class TaskScheduleCreate(BaseModel):
    """创建任务调度。cron / event 二选一（按 trigger_type 校验）。"""

    name: str = Field(..., min_length=1, max_length=100, description="调度名，全局唯一")
    trigger_type: str = Field(..., description="cron / event")
    cron_expr: str = Field(default="", description="cron 表达式（trigger_type=cron 必填）")
    event_source: str = Field(default="", description="事件来源标识（trigger_type=event 必填）")
    task_template: dict = Field(
        default_factory=dict,
        description='{"goal": "...", "plan": [...]}；goal 支持 {payload.xxx} 插值',
    )
    user_id: int = Field(..., description="执行身份（User.id，权限重算基准）")
    is_enabled: bool = True


class TaskScheduleUpdate(BaseModel):
    """更新任务调度（PATCH 语义：仅传入字段生效）。"""

    name: str | None = None
    trigger_type: str | None = None
    cron_expr: str | None = None
    event_source: str | None = None
    task_template: dict | None = None
    user_id: int | None = None
    is_enabled: bool | None = None


class TaskScheduleOut(BaseModel):
    """任务调度响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    trigger_type: str
    cron_expr: str = ""
    event_source: str = ""
    task_template: dict = Field(default_factory=dict)
    user_id: int
    is_enabled: bool = True
    last_fired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
