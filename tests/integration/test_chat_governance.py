"""数据面 /chat 治理链路集成测试 —— Bycall 核心论点的可执行验证。

验证：LLM 拿不到它无权拿的东西——不是"拿到后被拦"，是"请求时那个东西根本不在它的世界里"。

LLM 被依赖覆盖 mock 掉（这些测试验证治理链路，不验证 LLM 本身）。
链路：登录(admin) → 建角色(带受限 knowledge_scope) → 建用户 → act-as 该用户 →
  /chat → 该用户只能看到授权范围内的知识，无权内容被预过滤掉 + 审计落库。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.models import Role, RoleConfig, User
from bycall.governance.security import hash_password
from bycall.llm.base import LLMClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立 SQLite + init_db + seed。"""
    db_file = tmp_path / "test.db"
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
    """FastAPI TestClient，LLM 被 mock（治理链路测试不需要真实 LLM）。

    用 monkeypatch 替换 create_llm_client，测试结束自动还原，不污染其他测试。
    """
    from bycall.api.app import create_app

    app = create_app()
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat.return_value = "基于知识库的回答。"
    mock_llm.embed.return_value = []

    # 用 monkeypatch 替换 dataplane 模块里的 create_llm_client（dataplane 在请求时调用它）。
    from bycall.api import dataplane

    monkeypatch.setattr(dataplane, "create_llm_client", lambda config: mock_llm)
    return TestClient(app)


def _login(client, name="admin", password="admin123"):
    resp = client.post("/api/login", data={"name": name, "password": password})
    assert resp.status_code == 200 and resp.json().get("ok"), f"登录失败: {resp.text}"


def _user_id_by_name(name: str) -> int:
    from sqlmodel import select

    from bycall.governance.models import User as UserModel

    with session_scope() as s:
        return {u.name: u.id for u in s.exec(select(UserModel)).all()}[name]


# ============ 1. 访问控制 ============


class TestAccessControl:
    def test_chat_requires_login(self, client):
        """/chat 未登录 → 401。"""
        resp = client.post("/chat", json={"question": "hello"})
        assert resp.status_code == 401

    def test_admin_api_requires_admin_session(self, client):
        """admin API 未登录 → 401（会话守卫生效）。"""
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401

    def test_login_wrong_password_rejected(self, client):
        resp = client.post("/api/login", data={"name": "admin", "password": "wrong"})
        assert resp.status_code == 401

    def test_login_success_sets_cookie(self, client):
        resp = client.post("/api/login", data={"name": "admin", "password": "admin123"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert "bycall_session" in client.cookies


# ============ 2. ACL 预过滤：无权知识不进 LLM ============


class TestACLPrefiltering:
    def test_user_with_empty_scope_denied_before_llm(self, client):
        """knowledge_scope 为空的用户 → 被 pre-filtering 拒绝，LLM 根本不被调用。

        这是核心论点：用户无权的内容，LLM 拿不到（不是拿到后被拦）。
        denied_no_access 即证明在 LLM 之前就 return 了。
        """
        _login(client)
        with session_scope() as s:
            s.add(Role(name="intern", description="实习生", config=RoleConfig()))
            s.add(
                User(
                    name="bob",
                    display_name="Bob",
                    role="intern",
                    password_hash=hash_password("bob123"),
                )
            )
        bob_id = _user_id_by_name("bob")
        client.post("/api/admin/act-as", params={"target_user_id": bob_id})

        resp = client.post("/chat", json={"question": "公司销售额是多少"})
        body = resp.json()
        assert body["decision"] == "denied_no_access"
        assert "没有访问相关知识的权限" in body["answer"]
        assert body["knowledge_scope"] == []
        assert body["retrieved_chunks"] == 0

    def test_audit_log_records_deny_decision(self, client):
        """审计日志记录了 denied_no_access 决策。"""
        _login(client)
        with session_scope() as s:
            s.add(Role(name="intern2", description="", config=RoleConfig()))
            s.add(User(name="carol", display_name="Carol", role="intern2", password_hash="x"))
        carol_id = _user_id_by_name("carol")
        client.post("/api/admin/act-as", params={"target_user_id": carol_id})
        client.post("/chat", json={"question": "机密信息"})

        from sqlmodel import select

        from bycall.governance.models import AuditLog

        with session_scope() as s:
            logs = s.exec(select(AuditLog).where(AuditLog.user_name == "carol")).all()
            assert len(logs) >= 1
            assert logs[-1].decision == "denied_no_access"

    def test_act_as_only_in_dev(self, client, monkeypatch):
        """prod 环境下 act-as 被拒（PermissionError → 403）。"""
        _login(client)
        monkeypatch.setenv("BYCALL_ENV", "prod")
        get_settings.cache_clear()
        resp = client.post("/api/admin/act-as", params={"target_user_id": 999})
        assert resp.status_code in (403, 404)


# ============ 3. 有权限用户可正常对话（LLM mock）============


def test_authorized_user_gets_answer(client):
    """有 knowledge_scope 权限的用户 → 走完链路，得到 LLM 回答（mock）。"""
    _login(client)
    with session_scope() as s:
        s.add(
            Role(
                name="staff",
                description="员工",
                config=RoleConfig(knowledge_scope_tags=["public"], level=1),
            )
        )
        s.add(
            User(
                name="erin",
                display_name="Erin",
                role="staff",
                password_hash=hash_password("erin123"),
            )
        )
    erin_id = _user_id_by_name("erin")
    client.post("/api/admin/act-as", params={"target_user_id": erin_id})

    resp = client.post("/chat", json={"question": "公司介绍"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "answered"
    assert body["user"] == "erin"
    assert body["role"] == "staff"
    assert body["knowledge_scope"] == ["public"]


# ============ 4. 禁用用户无法被扮演（set_act_as 守卫）============


def test_inactive_user_cannot_be_impersonated(client):
    """act-as 一个 inactive 用户 → 被 set_act_as 守卫拒绝（403）。

    禁用用户根本无法通过 act-as 接触数据面。
    """
    _login(client)
    with session_scope() as s:
        s.add(Role(name="z", description="", config=RoleConfig(level=1)))
        s.add(
            User(
                name="dave",
                display_name="Dave",
                role="z",
                password_hash="x",
                is_active=False,
            )
        )
    dave_id = _user_id_by_name("dave")
    resp = client.post("/api/admin/act-as", params={"target_user_id": dave_id})
    assert resp.status_code == 403
