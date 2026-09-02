"""一键灌入「参考场景包」（ADR-0009，幂等）。

内容：3 个示例角色（technician/supervisor/guest）+ 3 个 demo 账号 + 2 个示例
MCP 连接器注册（examples/connectors/ 的 demo-ticket / demo-inventory）。

定位：平台核心**场景无关**，本脚本灌入的一切都是可删可改的示例——采用者应
复制它改出自己的场景种子，而不是把业务概念塞进平台默认种子（db.py 只留最小默认）。

用法::

    make seed-reference
    # 或：PYTHONPATH=src python scripts/seed_reference.py

注意：若 API 服务已在运行，灌入后 MCP 连接器需重启/热重载才会挂载——重启服务，
或经控制台对对应 server 触发 reload。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlmodel import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTORS_DIR = REPO_ROOT / "examples" / "connectors"

# ---- 工具全名（必须与连接器内 server 名 + 工具名严格一致）----
_T_CREATE = "mcp__demo-ticket__create_ticket"
_T_QUERY = "mcp__demo-ticket__query_ticket"
_T_LIST = "mcp__demo-ticket__list_tickets"
_T_CLOSE = "mcp__demo-ticket__close_ticket"
_I_SEARCH = "mcp__demo-inventory__search_parts"
_I_STOCK = "mcp__demo-inventory__get_part_stock"
_I_RESERVE = "mcp__demo-inventory__reserve_part"

# ---- 平台级通用 Skill 占位（去行业化，演示 Skill 按角色分发）----
# RAG 占位说明：知识库文档待用户后续提供，本任务仅用 Skill 演示领域知识注入。
REFERENCE_SKILLS: list[dict] = [
    {
        "name": "platform-record-guide",
        "description": "通用记录操作指引（平台级示例 Skill）",
        "content": (
            "# 通用记录操作指引\n"
            "## 何时使用工具\n"
            "- 查询记录：`query_ticket` / `list_tickets` / `search_parts` / `get_part_stock`\n"
            "- 创建记录：`create_ticket`（需标题，缺参时追问）\n"
            "## 参数规范\n"
            "- `title` 不能为空，`priority` 可选 low/normal/high/urgent\n"
            "- 查询支持按 ID 精确查或关键词模糊搜\n"
            "## 约束\n"
            "- 只使用已授权工具，不要编造不存在的工具或记录\n"
        ),
        "disclosure": "always",
    },
    {
        "name": "platform-risk-note",
        "description": "高风险操作需审批说明（平台级示例 Skill）",
        "content": (
            "# 高风险操作需审批说明\n"
            "- 标记 `destructiveHint` 的工具（如 `close_ticket` / `reserve_part`）为高危操作，\n"
            "  需经人工审批后方可执行；未审批前回复“已提交审批”并说明原因。\n"
            "- 审批通过后系统会主动推送结果给发起人；授权分三档 once/session/always。\n"
        ),
        "disclosure": "always",
    },
]

# ---- 示例角色：能力差异即治理演示（guest 看不到任何工具）----
# 平台去行业化：角色名保留 technician/supervisor/guest 以兼容既有 demo 账号，
# 但描述与 Skill 绑定改为平台通用话术，不含行业词汇。
REFERENCE_ROLES: list[dict] = [
    {
        "name": "technician",
        "description": "示例：一线执行者——记录查询/创建 + 物料查询，无高危操作",
        "allowed_tools": [_T_CREATE, _T_QUERY, _T_LIST, _I_SEARCH, _I_STOCK],
        "knowledge_scope_tags": ["public", "ops"],
        "level": 1,
        "skills": ["platform-record-guide"],
    },
    {
        "name": "supervisor",
        "description": "示例：值班主管——一线全部能力 + 高危操作（关单/预留）",
        "allowed_tools": [_T_CREATE, _T_QUERY, _T_LIST, _T_CLOSE, _I_SEARCH, _I_STOCK, _I_RESERVE],
        "knowledge_scope_tags": ["public", "ops"],
        "level": 2,
        "skills": ["platform-record-guide", "platform-risk-note"],
    },
    {
        "name": "guest",
        "description": "示例：访客——最小权限，无任何工具（LLM 根本看不到工具）",
        "allowed_tools": [],
        "knowledge_scope_tags": ["public"],
        "level": 0,
        "skills": [],
    },
]

# ---- demo 账号（无密码：IM 用户；im_bindings 为企微 demo id，可改）----
REFERENCE_USERS: list[dict] = [
    {
        "name": "demo-tech",
        "display_name": "一线工程师（示例）",
        "role": "technician",
        "im_bindings": {"wecom": "DEMO_TECH_001"},
    },
    {
        "name": "demo-lead",
        "display_name": "值班主管（示例）",
        "role": "supervisor",
        "im_bindings": {"wecom": "DEMO_LEAD_001"},
    },
    {
        "name": "demo-guest",
        "display_name": "访客（示例）",
        "role": "guest",
        "im_bindings": {"wecom": "DEMO_GUEST_001"},
    },
]

# ---- MCP 连接器注册（stdio 子进程：用当前解释器跑示例脚本）----
REFERENCE_MCP_SERVERS: list[dict] = [
    {
        "name": "demo-ticket",
        "description": "示例连接器：demo 工单系统（examples/connectors/ticket_server.py）",
        "script": "ticket_server.py",
    },
    {
        "name": "demo-inventory",
        "description": "示例连接器：demo 备件库存（examples/connectors/inventory_server.py）",
        "script": "inventory_server.py",
    },
]


def seed_reference(session: Session) -> list[str]:
    """把参考场景灌入 DB（幂等）。返回人读的操作日志。"""
    from bycall.governance.models import MCPServer, Role, RoleConfig, Skill, User

    log: list[str] = []

    # 0. 平台级 Skill 占位（去行业化）：存在则刷新内容，幂等。
    for spec in REFERENCE_SKILLS:
        row = session.exec(Skill.select_by_name(spec["name"])).first()
        if row is None:
            session.add(
                Skill(
                    name=spec["name"],
                    description=spec["description"],
                    content=spec["content"],
                    disclosure=spec["disclosure"],
                    is_enabled=True,
                )
            )
            log.append(f"+ Skill {spec['name']}")
        else:
            row.description = spec["description"]
            row.content = spec["content"]
            row.disclosure = spec["disclosure"]
            row.is_enabled = True
            log.append(f"~ Skill {spec['name']}（刷新内容）")

    # 1. 角色：存在则刷新 config（连接器工具名变更后重跑即同步），新建则插入。
    for spec in REFERENCE_ROLES:
        role = session.get(Role, spec["name"])
        config = RoleConfig(
            allowed_tools=list(spec["allowed_tools"]),
            knowledge_scope_tags=list(spec["knowledge_scope_tags"]),
            level=spec["level"],
            skills=list(spec.get("skills", [])),
        )
        if role is None:
            session.add(Role(name=spec["name"], description=spec["description"], config=config))
            log.append(f"+ 角色 {spec['name']}")
        else:
            role.config = config
            role.description = spec["description"]
            log.append(f"~ 角色 {spec['name']}（刷新配置）")

    # 2. demo 账号：已存在则跳过（密码/IM 绑定可能已被管理员改过，不覆盖）。
    for spec in REFERENCE_USERS:
        if session.exec(User.select_by_name(spec["name"])).first() is None:
            session.add(
                User(
                    name=spec["name"],
                    display_name=spec["display_name"],
                    role=spec["role"],
                    im_bindings=dict(spec["im_bindings"]),
                )
            )
            log.append(f"+ 账号 {spec['name']}（角色 {spec['role']}）")
        else:
            log.append(f"= 账号 {spec['name']} 已存在，跳过")

    # 3. MCP 连接器注册：存在则刷新 command/args（脚本路径可能因仓库迁移变化）。
    for spec in REFERENCE_MCP_SERVERS:
        script_path = CONNECTORS_DIR / spec["script"]
        if not script_path.exists():
            raise FileNotFoundError(f"连接器脚本不存在：{script_path}")
        row = session.exec(MCPServer.select_by_name(spec["name"])).first()
        if row is None:
            session.add(
                MCPServer(
                    name=spec["name"],
                    description=spec["description"],
                    transport="stdio",
                    command=sys.executable,
                    args_json=json.dumps([str(script_path)]),
                )
            )
            log.append(f"+ MCP server {spec['name']}")
        else:
            row.command = sys.executable
            row.args_json = json.dumps([str(script_path)])
            row.description = spec["description"]
            log.append(f"~ MCP server {spec['name']}（刷新路径）")

    session.commit()
    return log


def main() -> int:
    from bycall.db import init_db, session_scope

    init_db()  # 幂等：建表 + 最小默认种子
    with session_scope() as session:
        log = seed_reference(session)

    print("参考场景包已灌入（幂等，可重复执行）：")
    for line in log:
        print(f"  {line}")
    print(
        "\n下一步：\n"
        "  1. make dev 启动服务（MCPManager 会拉起两个示例连接器并发现工具）\n"
        "  2. 用 demo-tech / demo-lead / demo-guest 体验能力差异（guest 看不到任何工具）\n"
        "  3. 复制 scripts/seed_reference.py 与 examples/connectors/ 改造为你自己的场景\n"
        "     （ADR-0009：平台场景无关，示例可整体删除不影响平台）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
