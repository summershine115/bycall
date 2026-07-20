"""SKILL 注入端到端测试：验证角色绑定的 skill 真的进 LLM 的 system 段。

覆盖 SKILL 注入主链路（admin 建 skill → 角色绑定 → /chat → LLM system 段含 skill）：
- 角色绑定 skill → /chat → LLM 的 system 段含 skill 内容 + "领域技能" + "非指令"标注
- 多 skill 全部注入，格式 ## 技能：{name}
- 未绑定 skill → system 段无"领域技能"小节
- admin → 无 skill 段（运维角色不注入）

提示词注入防御（本测试固化的两级隔离）：
- skill 进 system 角色（admin 管控的可信数据，靠"非指令"标注软隔离）
- summary/RAG 进 user 角色（不可信，硬隔离）

范式与 test_user_memory_injection.py 一致：mock LLM + call_log 捕获 + admin 登录 + act-as。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.llm import resolver


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """干净环境：SQLite + zvec + dev 模式。"""
    db_file = tmp_path / "test_skill_inject.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_file}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False}))
    init_db()
    return tmp_path


@pytest.fixture
def client(env, monkeypatch):
    """FastAPI TestClient，LLM 被 mock，``call_log`` 捕获每次收到的 messages。登录 admin。"""
    mock_llm = MagicMock(spec=BaseChatModel)
    call_log: list[list] = []

    def invoke(messages, **kwargs):
        call_log.append(messages)
        return AIMessage(content="skill 测试回答")

    mock_llm.invoke.side_effect = invoke
    mock_llm.bind_tools.return_value = mock_llm
    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(resolver, "get_chat_model", lambda session: (fake_model, mock_llm))

    app = create_app()
    c = TestClient(app)

    resp = c.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert resp.status_code == 200
    return c, call_log


def _make_user_with_skill_role(client: TestClient, name: str, skill_names: list[str]) -> int:
    """建一个绑定 skills 的角色 + 用户，返回 user id。

    角色需带 knowledge_scope_tags（否则 member 被 denied_no_access 拒在 LLM 之前）。
    """
    role_name = f"role_{name}"
    client.post(
        "/api/admin/roles",
        json={
            "name": role_name,
            "description": "skill 测试角色",
            "config": {
                "allowed_tools": [],
                "knowledge_scope_tags": ["public"],
                "level": 1,
                "skills": skill_names,
            },
        },
    )
    client.post("/api/admin/users", json={"name": name, "role": role_name, "is_active": True})
    users = client.get("/api/admin/users").json()
    return next(u["id"] for u in users if u["name"] == name)


def _system_text(messages: list) -> str:
    """从 LLM 收到的 messages 里取 system 段文本。"""
    for m in messages:
        if getattr(m, "type", None) == "system" or "system" in m.__class__.__name__.lower():
            return m.content
    return ""


class TestSkillInjection:
    def test_bound_skill_injected_into_system(self, client):
        """角色绑定 skill → /chat → LLM system 段含 skill 内容 + 边界标注。"""
        c, call_log = client

        # 建 skill
        c.post(
            "/api/admin/skills",
            json={
                "name": "sales-playbook",
                "description": "销售话术",
                "content": "客户咨询时先确认需求再报价",
            },
        )

        # 建绑定该 skill 的用户
        uid = _make_user_with_skill_role(c, "alice", ["sales-playbook"])

        # 以 alice 身份 /chat
        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你好"})
        c.post("/api/admin/clear-act-as")

        sys_text = _system_text(call_log[0])
        # skill 内容进 system 段
        assert "客户咨询时先确认需求再报价" in sys_text
        # 带边界标注（领域技能 + 非指令，软隔离）
        assert "领域技能" in sys_text
        assert "非指令" in sys_text
        # 格式 ## 技能：sales-playbook
        assert "## 技能：sales-playbook" in sys_text

    def test_multiple_skills_all_injected(self, client):
        """角色绑定多个 skill → 全部注入 system 段。"""
        c, call_log = client

        for name, content in [("skill-a", "内容A"), ("skill-b", "内容B")]:
            c.post(
                "/api/admin/skills",
                json={"name": name, "description": "", "content": content},
            )

        uid = _make_user_with_skill_role(c, "bob", ["skill-a", "skill-b"])
        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你好"})
        c.post("/api/admin/clear-act-as")

        sys_text = _system_text(call_log[0])
        assert "## 技能：skill-a" in sys_text
        assert "内容A" in sys_text
        assert "## 技能：skill-b" in sys_text
        assert "内容B" in sys_text

    def test_no_skill_no_section(self, client):
        """角色未绑定 skill → system 段无"领域技能"小节。"""
        c, call_log = client
        uid = _make_user_with_skill_role(c, "carol", [])  # 无 skill 绑定

        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你好"})
        c.post("/api/admin/clear-act-as")

        sys_text = _system_text(call_log[0])
        assert "领域技能" not in sys_text
        # system 段本身存在（含 system_prompt + 行为规则）
        assert len(sys_text) > 0

    def test_disabled_skill_not_injected(self, client):
        """绑定的 skill 被禁用 → 不注入（静默忽略，不报错）。"""
        c, call_log = client

        c.post(
            "/api/admin/skills",
            json={"name": "disabled-one", "description": "", "content": "禁用内容"},
        )
        # 禁用它
        c.put("/api/admin/skills/disabled-one", json={"is_enabled": False})

        uid = _make_user_with_skill_role(c, "dave", ["disabled-one"])
        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你好"})
        c.post("/api/admin/clear-act-as")

        sys_text = _system_text(call_log[0])
        assert "禁用内容" not in sys_text
        assert "领域技能" not in sys_text

    def test_admin_no_skill_section(self, client):
        """admin 直接 /chat → 无 skill 段（运维角色不注入）。"""
        c, call_log = client
        c.post(
            "/api/admin/skills",
            json={"name": "any", "description": "", "content": "任何内容"},
        )
        # admin 直接提问（不 act-as）
        c.post("/chat", json={"question": "你好"})

        sys_text = _system_text(call_log[0])
        assert "领域技能" not in sys_text
        assert "任何内容" not in sys_text
