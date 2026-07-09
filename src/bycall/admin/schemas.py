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

from pydantic import BaseModel, ConfigDict, Field

from bycall.governance.models import RoleConfig

# ============ 用户 ============


class UserCreate(BaseModel):
    """创建用户请求。

    ``password`` 可选——不传时由 admin 在控制面后续设置（或 IM 用户根本不需要密码）。
    """

    name: str = Field(..., description="登录名/业务标识，全局唯一")
    display_name: str = Field(default="", description="显示名")
    role: str = Field(default="member", description="角色名（Role.name 或内置 admin）")
    password: str | None = Field(
        default=None, description="明文密码，不传则留空（IM 用户无需密码）"
    )
    is_active: bool = Field(default=True, description="是否启用")


class UserUpdate(BaseModel):
    """更新用户请求（PATCH 语义：仅传入字段生效）。

    传 ``password`` 表示改密码；传 ``role`` 会校验目标角色存在。
    """

    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, description="传非空值即改密码")


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
    created_at: datetime
