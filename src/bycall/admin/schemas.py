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


class SkillUpdate(BaseModel):
    """更新 Skill（PATCH 语义，只改传入字段）。可迭代文档，无需发新版本。"""

    description: str | None = None
    content: str | None = None
    is_enabled: bool | None = None


class SkillOut(BaseModel):
    """Skill 响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    content: str
    is_enabled: bool
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
    created_at: datetime


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
    """

    name: str = Field(..., description="知识库名称，全局唯一")
    description: str = Field(default="", description="描述")
    embedding_model_id: int = Field(..., description="绑定的 embedding 模型 ID（决定维度）")


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库（PATCH 语义）。只改名称/描述，不改 embedding 模型（维度锁定）。"""

    name: str | None = None
    description: str | None = None


class KnowledgeBaseOut(BaseModel):
    """知识库响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    embedding_model_id: int
    dimension: int
    collection_name: str
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
