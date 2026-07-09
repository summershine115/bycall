"""治理领域模型（SQLModel 表）。

这是整个权限系统的"真值"所在——admin 在控制面修改这些记录，
数据面的每次交互都据此计算能力集。LLM 碰不到这层。

表关系：
- User ──> Role（多对一，用户属于一个角色）
- Role ──内嵌──> RoleConfig（该角色的工具白名单 / 知识域 / 职级）
- SystemPrompt（版本化，激活版本下发给数据面）
- AuditLog（append-only，谁/何时/问什么/放行/拒绝）

⚠️ 设计约定：
- ``admin`` 是内置特殊角色名（不建 Role 表记录），policy 引擎识别后给全部权限。
- ``knowledge_scope_tags`` 是知识域标签（如 ["sales","public"]），RAG 预过滤时
  翻译成 ``source IN (...)`` 形式的 filter。M2 接 ACL 管理界面后可细化到文档级。
- ``level`` 是职级（ABAC 用），0=普通，越大权限越高（机密文件按 ``level <= user_level`` 放行）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel

from bycall.governance.json_types import DictJSONType, PydanticJSONType


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
    """

    allowed_tools: list[str] = Field(default_factory=list)
    knowledge_scope_tags: list[str] = Field(default_factory=list)
    level: int = 0
    # 预留：M3 接入时加 max_iterations / rate_limit 等。


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


# ============ AuditLog ============


class AuditLog(SQLModel, table=True):
    """审计日志（append-only）。每次数据面交互都落一条。

    记录：谁、何时、问了什么、被放行/拒绝了什么、LLM 回了什么。
    合规与事后排查的命脉。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, index=True)
    user_name: str = Field(default="", index=True)
    channel: str = Field(default="api")  # im/api/admin
    action: str = Field(index=True)  # chat / tool_call / auth / deny
    question: str = ""
    decision: str = ""  # allowed / denied / error
    detail: str = ""  # 拒绝原因 / 工具名 / 检索命中等
    answer: str = ""
    created_at: datetime = Field(
        default_factory=_now_utc,
        sa_column=Column(DateTime, default=_now_utc, index=True),
    )

    @staticmethod
    def select_recent(limit: int = 100):
        from sqlmodel import select

        return select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
