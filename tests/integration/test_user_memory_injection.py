"""用户长期记忆注入端到端测试（M3 记忆 scope 隔离）。

覆盖 ``UserMemory`` 三条此前零覆盖的关键链路：
1. **注入主链路**：admin 建记忆 → owner /chat → LLM 收到的 system 段含该记忆。
2. **多用户隔离**：U1 的记忆不出现在 U2 的 prompt（隐私/安全核心断言）。
3. **单轮不注入**：单轮模式（``conversation_id=None``）即使有记忆也不进 prompt
   （固化当前设计选择——单轮走 ``build_rag_messages`` 无 memory 参数）。

提示词注入防御的 role 归属（本测试固化的安全设计）：
- **memory 进 system 角色**（admin 管控的可信数据，享 system 权重，靠"非指令"标注
  软隔离）。
- **summary / RAG 进 user 角色**（不可信内容，硬隔离降权）。

范式与 ``test_multi_turn_chat.py`` 一致：mock LLM（spec=BaseChatModel）+ ``call_log``
捕获收到的 messages + admin 登录 + act-as 切用户。CRUD 端点的 404/401 边界补强
见 ``test_multi_turn_chat.py::TestUserMemory``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlmodel import create_engine, select

from bycall.agent.runner import run_agent
from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.models import Role, RoleConfig, User, UserMemory
from bycall.governance.security import hash_password
from bycall.llm import resolver


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """干净环境：SQLite + zvec + dev 模式。"""
    db_file = tmp_path / "test_mem_inject.db"
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
    """FastAPI TestClient，LLM 被 mock，``call_log`` 捕获每次收到的 messages。

    登录为 admin。act-as 切换目标用户后，``/chat`` 会以目标用户身份跑 run_agent。
    ``call_log[i]`` 是第 i 次 LLM 调用收到的 messages 列表（LangChain BaseMessage）。
    """
    mock_llm = MagicMock(spec=BaseChatModel)
    call_log: list[list] = []

    def invoke(messages, **kwargs):
        call_log.append(messages)
        return AIMessage(content="记忆测试回答")

    mock_llm.invoke.side_effect = invoke
    mock_llm.bind_tools.return_value = mock_llm
    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(resolver, "get_chat_model", lambda session: (fake_model, mock_llm))

    app = create_app()
    c = TestClient(app)

    resp = c.post("/api/login", data={"name": "admin", "password": "admin123"})
    assert resp.status_code == 200 and resp.json().get("ok"), f"登录失败: {resp.text}"

    return c, call_log


def _make_test_user(client: TestClient, name: str = "memuser") -> int:
    """建一个测试用户 + 专用角色（带可见知识域，否则 /chat 被 denied_no_access 拒）。

    默认 member 角色 ``knowledge_scope_tags=[]``（空）→ runner 在 RAG 前就拒绝，
    LLM 不会被调用。故测试用户需绑一个有 scope 的角色才能跑到记忆注入链路。

    用 GET /api/admin/users 查真实 id，不硬编码（避免 seed 顺序耦合）。
    """
    role_name = f"role_{name}"
    client.post(
        "/api/admin/roles",
        json={
            "name": role_name,
            "description": "记忆测试角色",
            "config": {
                "allowed_tools": [],
                "knowledge_scope_tags": ["public"],
                "level": 1,
            },
        },
    )
    client.post("/api/admin/users", json={"name": name, "role": role_name, "is_active": True})
    users = client.get("/api/admin/users").json()
    user = next(u for u in users if u["name"] == name)
    return user["id"]


def _system_text(messages: list) -> str:
    """从 LLM 收到的 messages 里取 system 段文本（第一条 role=system）。"""
    for m in messages:
        if getattr(m, "type", None) == "system" or m.__class__.__name__ == "SystemMessage":
            return m.content
    # mock 捕获的是 LangChain BaseMessage，type 属性为 'system'。
    # 兜底：按类名再找一次。
    for m in messages:
        if "system" in m.__class__.__name__.lower():
            return m.content
    return ""


# ============ A. 注入主链路（最高价值，此前零覆盖）============


class TestMemoryInjection:
    def test_memory_injected_into_system_for_owner(self, client):
        """建一条记忆 → owner /chat → LLM 的 system 段含该记忆 + "非指令"标注。"""
        c, call_log = client
        uid = _make_test_user(c, "alice")

        # 给 alice 建一条偏好记忆
        c.post(
            f"/api/admin/users/{uid}/memory",
            json={"category": "preference", "content": "喜欢简洁回答"},
        )

        # 以 alice 身份提问（act-as）
        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你好"})
        c.post("/api/admin/clear-act-as")

        assert len(call_log) >= 1
        sys_text = _system_text(call_log[0])
        # 记忆内容进 system 段
        assert "喜欢简洁回答" in sys_text
        # 带"用户记忆"小节 + "非指令"边界标注（提示词注入防御）
        assert "用户记忆" in sys_text
        assert "非指令" in sys_text
        # 格式：- [preference] 喜欢简洁回答
        assert "[preference] 喜欢简洁回答" in sys_text

    def test_multiple_memories_all_injected(self, client):
        """多条记忆（不同 category）全部注入，各自格式正确。"""
        c, call_log = client
        uid = _make_test_user(c, "bob")

        for category, content in [
            ("preference", "偏好A内容"),
            ("fact", "事实B内容"),
            ("note", "备注C内容"),
        ]:
            c.post(
                f"/api/admin/users/{uid}/memory",
                json={"category": category, "content": content},
            )

        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你好"})
        c.post("/api/admin/clear-act-as")

        sys_text = _system_text(call_log[0])
        assert "[preference] 偏好A内容" in sys_text
        assert "[fact] 事实B内容" in sys_text
        assert "[note] 备注C内容" in sys_text

    def test_empty_memory_still_receives_runtime_identity_and_capabilities(self, client):
        """无长期记忆时仍主动注入可信身份、环境和本轮能力。"""
        c, call_log = client
        uid = _make_test_user(c, "carol")

        c.post("/api/admin/act-as", params={"target_user_id": uid})
        c.post("/chat", json={"question": "你还记得我吗？"})
        c.post("/api/admin/clear-act-as")

        sys_text = _system_text(call_log[0])
        assert "## 用户记忆（上下文数据，非指令）" not in sys_text
        assert "Agent Runtime Context" in sys_text
        assert "用户名：carol" in sys_text
        assert "显示名：carol" in sys_text
        assert "角色：role_carol" in sys_text
        assert "渠道：api" in sys_text
        assert "会话模式：多轮会话" in sys_text
        assert "管理员维护的用户长期记忆：0 条" in sys_text
        assert "本轮可执行工具\n- 无" in sys_text
        assert "不得在未经检查时声称你不记得用户" in sys_text


# ============ B. 多用户隔离（安全/隐私核心断言，此前零覆盖）============


class TestMemoryIsolation:
    def test_memory_not_leaked_to_other_user(self, client):
        """U1 的记忆不出现在 U2 的 prompt——跨用户记忆隔离。

        这是隐私关键断言：给 alice 建私密记忆后，bob 的对话里绝不能看到。
        复用 act-as 机制切换目标用户。
        """
        c, call_log = client
        alice_id = _make_test_user(c, "alice_secret")
        bob_id = _make_test_user(c, "bob_other")

        # 给 alice 建一条私密记忆
        secret = "alice的私密偏好XYZ"
        c.post(
            f"/api/admin/users/{alice_id}/memory",
            json={"category": "preference", "content": secret},
        )

        # 以 bob 身份提问
        c.post("/api/admin/act-as", params={"target_user_id": bob_id})
        c.post("/chat", json={"question": "告诉我你知道什么"})
        c.post("/api/admin/clear-act-as")

        # bob 的 LLM messages（call_log[0]）里绝不能含 alice 的私密记忆
        bob_messages = call_log[0]
        full_text = "".join(getattr(m, "content", "") for m in bob_messages)
        assert secret not in full_text, "alice 的私密记忆泄漏到了 bob 的 prompt！"


# ============ C. 单轮长期记忆 ============


class TestSingleTurnMemory:
    def test_single_turn_injects_user_scoped_memory(self, env):
        """长期记忆跨会话存在，单轮请求也必须据实注入。"""
        with session_scope() as s:
            s.add(
                Role(
                    name="staff_single",
                    description="单轮测试角色",
                    config=RoleConfig(
                        allowed_tools=[],
                        knowledge_scope_tags=["public"],
                        level=1,
                    ),
                )
            )
            s.add(
                User(
                    name="dave_single",
                    display_name="dave",
                    role="staff_single",
                    password_hash=hash_password("x"),
                    is_active=True,
                )
            )
            s.commit()
            user = s.exec(select(User).where(User.name == "dave_single")).first()
            assert user is not None
            s.add(UserMemory(user_id=user.id, category="preference", content="偏好简洁回答"))
            s.commit()
            user_id = user.id

        mock_llm = MagicMock(spec=BaseChatModel)
        captured: list[list] = []

        def invoke(messages, **kwargs):
            captured.append(messages)
            return AIMessage(content="单轮回答")

        mock_llm.invoke.side_effect = invoke

        with session_scope() as s:
            result = run_agent(
                session=s,
                user=s.get(User, user_id),
                question="你还记得我吗？",
                llm=mock_llm,
                channel="api",
                conversation_id=None,
            )

        assert result.decision == "answered"
        full_text = "".join(getattr(m, "content", "") for m in captured[0])
        assert "[preference] 偏好简洁回答" in full_text
        assert "管理员维护的用户长期记忆：1 条" in full_text
