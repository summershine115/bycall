"""治理脊柱：身份 → 角色 → 权限矩阵 → 当前会话能力集。

这是 Bycall 区别于个人 Agent 产品的核心——策略活在代码层，不在提示词层。
LLM 只能"请求"，中间件/检索层/工具网关"判定"是否放行。

子模块：
- ``models``：领域对象（User/Role/RoleConfig/SystemPrompt/AuditLog）
- ``security``：密码哈希、会话签名
- ``capability``：策略引擎，把「用户+角色」翻译成「当前能力集」
- ``policy``：FastAPI 依赖，会话级权限中间件
"""

from bycall.governance import models

__all__ = ["models"]
