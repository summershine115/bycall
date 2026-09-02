"""参考场景包种子脚本测试（scripts/seed_reference.py）。

验证：
- 幂等（重复执行不产生重复行、不报错），角色/连接器刷新而非重复插入；
- 种子内容与治理契约一致：guest 无任何工具、technician 无高危工具、supervisor 全量；
- 能力集按白名单生成（LLM 可见性由角色决定，不是"看到不让调"）；
- 注册的 MCPServer 指向真实存在的连接器脚本（stdio 路径有效）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.governance.capability import (
    clear_capability_cache,
    compute_capability_set,
)
from bycall.governance.models import MCPServer, Role, User

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_seed_module():
    path = _REPO_ROOT / "scripts" / "seed_reference.py"
    spec = importlib.util.spec_from_file_location("seed_reference", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    from bycall.db import session_scope

    with session_scope() as s:
        yield s
    clear_capability_cache()


def test_seed_idempotent_and_contents(session):
    seed = _load_seed_module()

    log1 = seed.seed_reference(session)
    # 幂等：第二次执行角色/MCP 刷新（~），账号跳过（=），不产生重复。
    log2 = seed.seed_reference(session)
    assert sum(1 for line in log2 if line.startswith("+")) == 0
    # 3 角色 + 3 账号 + 2 server + 2 平台级 Skill（去行业化）
    assert sum(1 for line in log1 if line.startswith("+")) == 10

    from sqlmodel import select

    roles = {r.name: r for r in session.exec(select(Role)).all()}
    # 默认 member + 3 个示例角色。
    assert {"member", "technician", "supervisor", "guest"} <= set(roles)

    # 平台级 Skill 占位已灌入
    from bycall.governance.models import Skill

    skills = {s.name: s for s in session.exec(select(Skill)).all()}
    assert "platform-record-guide" in skills
    assert "platform-risk-note" in skills

    guest = roles["guest"].config
    assert guest.allowed_tools == []  # 访客：LLM 根本看不到工具
    assert guest.skills == []
    tech = roles["technician"].config
    assert "mcp__demo-ticket__close_ticket" not in tech.allowed_tools
    assert "mcp__demo-inventory__search_parts" in tech.allowed_tools
    assert tech.skills == ["platform-record-guide"]
    lead = roles["supervisor"].config
    assert set(tech.allowed_tools) < set(lead.allowed_tools)
    assert "mcp__demo-ticket__close_ticket" in lead.allowed_tools
    assert "mcp__demo-inventory__reserve_part" in lead.allowed_tools
    assert set(lead.skills) == {"platform-record-guide", "platform-risk-note"}

    # demo 账号：角色映射正确。
    users = {u.name: u for u in session.exec(select(User)).all()}
    assert users["demo-tech"].role == "technician"
    assert users["demo-lead"].role == "supervisor"
    assert users["demo-guest"].role == "guest"
    assert users["demo-tech"].im_bindings["wecom"] == "DEMO_TECH_001"

    # MCP server 注册：stdio + 脚本路径真实存在。
    servers = {s.name: s for s in session.exec(select(MCPServer)).all()}
    assert set(servers) == {"demo-ticket", "demo-inventory"}
    for srv in servers.values():
        assert srv.transport == "stdio" and srv.is_enabled
        script = Path(json.loads(srv.args_json)[0])
        assert script.exists() and script.parent.name == "connectors"


def test_capability_set_follows_reference_roles(session):
    seed = _load_seed_module()
    seed.seed_reference(session)

    from sqlmodel import select

    for name, expect_len, must_have, must_not in [
        ("demo-guest", 0, None, "mcp__demo-inventory__search_parts"),
        ("demo-tech", 5, "mcp__demo-ticket__create_ticket", "mcp__demo-ticket__close_ticket"),
        ("demo-lead", 7, "mcp__demo-ticket__close_ticket", None),
    ]:
        user = session.exec(select(User).where(User.name == name)).one()
        cap = compute_capability_set(session, user)
        assert len(cap.allowed_tools) == expect_len
        if must_have:
            assert must_have in cap.allowed_tools
        if must_not:
            assert must_not not in cap.allowed_tools
