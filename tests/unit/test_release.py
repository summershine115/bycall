"""发布门 + 灰度 + 回滚测试（阶段 2 T2.4）。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import create_engine

from bycall.admin.api import router
from bycall.config import get_settings
from bycall.db import init_db, set_engine
from bycall.evals.core import CaseResult, EvalReport
from bycall.evals.gate import GateReport
from bycall.evals.harness import eval_session
from bycall.governance.models import Conversation, Model, ReleaseState, SystemPrompt
from bycall.governance.release import (
    in_canary,
    promote_release,
    resolve_canary_chat_model,
    resolve_prompt_content_for_user,
    rollback_release,
    upsert_release,
)

# ============ 治理层：灰度路由 / 回滚 ============


def test_in_canary_stable_and_bounds():
    """稳定哈希：边界 0/100 全否/全是，同一 scope 多次结果一致。"""
    assert in_canary(42, 0) is False
    assert in_canary(42, 100) is True
    assert in_canary(42, 50) == in_canary(42, 50)


def test_prompt_canary_resolution():
    """prompt 灰度：按用户稳定哈希分流；未命中走旧版。"""
    with eval_session() as session:
        session.add(SystemPrompt(version=1, content="OLD_PROMPT", is_active=True))
        session.add(SystemPrompt(version=2, content="NEW_PROMPT", is_active=False))
        session.add(
            ReleaseState(
                kind="prompt",
                target_prompt_version=2,
                previous_prompt_version=1,
                canary_percent=0,
                status="canary",
            )
        )
        session.commit()

        assert resolve_prompt_content_for_user(session, 1) == "OLD_PROMPT"

        release = session.get(ReleaseState, "prompt")
        release.canary_percent = 100
        session.add(release)
        session.commit()
        assert resolve_prompt_content_for_user(session, 1) == "NEW_PROMPT"


def test_promote_then_rollback_switches_is_active():
    """灰度 → 全量 → 回滚：is_active 全程复用 version + is_active 模式。"""
    with eval_session() as session:
        session.add(SystemPrompt(version=1, content="OLD", is_active=True))
        session.add(SystemPrompt(version=2, content="NEW", is_active=False))
        session.add(
            ReleaseState(
                kind="prompt",
                target_prompt_version=2,
                previous_prompt_version=1,
                canary_percent=50,
                status="canary",
            )
        )
        session.commit()

        promote_release(session, "prompt")
        session.commit()
        active = session.exec(SystemPrompt.select_active()).first()
        assert active is not None and active.version == 2
        assert session.get(ReleaseState, "prompt").status == "active"

        rollback_release(session, "prompt")
        session.commit()
        active = session.exec(SystemPrompt.select_active()).first()
        assert active is not None and active.version == 1
        assert session.get(ReleaseState, "prompt").status == "rolled_back"


def test_model_canary_routing_with_per_session_priority(monkeypatch):
    """模型灰度：无 per-session 绑定的会话按稳定哈希分流；绑定仍优先。"""
    with eval_session() as session:
        session.add(
            Model(
                name="chat-old",
                model_type="chat",
                source="remote",
                provider="generic",
                base_url="https://old.example/v1",
                api_key="sk-old",
                is_active=True,
            )
        )
        session.add(
            Model(
                name="chat-new",
                model_type="chat",
                source="remote",
                provider="generic",
                base_url="https://new.example/v1",
                api_key="sk-new",
                is_active=False,
            )
        )
        session.commit()
        old = session.exec(Model.select_by_type("chat").where(Model.name == "chat-old")).first()
        new = session.exec(Model.select_by_type("chat").where(Model.name == "chat-new")).first()
        upsert_release(
            session,
            kind="model",
            target_model_id=new.id,
            previous_model_id=old.id,
            canary_percent=50,
            gate_report_json="{}",
        )
        conv = Conversation(user_id=1, channel="api", session_key="api:canary:1")
        session.add(conv)
        session.commit()

        from bycall.llm import resolver

        # 让该会话必然落入灰度桶（稳定哈希的边界/稳定性已在上个用例单独验证）。
        monkeypatch.setattr("bycall.governance.release.in_canary", lambda scope_id, percent: True)
        monkeypatch.setattr(
            resolver, "resolve_chat_model", lambda session, model, timeout=60.0: f"fake:{model.id}"
        )
        entity, _resolved = resolver.get_chat_model_for_conversation(session, conv)
        assert entity.id == new.id

        # per-session 模型绑定优先于 canary。
        conv.model_id = old.id
        session.add(conv)
        session.commit()
        entity2, _resolved2 = resolver.get_chat_model_for_conversation(session, conv)
        assert entity2.id == old.id

        # 无会话（单轮）不走 canary，仍用全局激活。
        entity3 = resolve_canary_chat_model(session, None)
        assert entity3 is None


# ============ 控制面：发布门 / 灰度 / 回滚端点 ============


@pytest.fixture
def client(tmp_path, monkeypatch):
    """挂了 admin router 的 TestClient + 全新 SQLite（同 test_admin_api 模式）。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _create_prompt(client: TestClient, version: int, content: str = "CONTENT") -> None:
    resp = client.post(
        "/api/admin/prompts", json={"version": version, "label": f"v{version}", "content": content}
    )
    assert resp.status_code == 201, resp.text


def _active_prompt_version(client: TestClient) -> int:
    prompts = client.get("/api/admin/prompts").json()
    return next(p["version"] for p in prompts if p["is_active"])


def test_gate_failure_blocks_publish_without_side_effect(client, monkeypatch):
    """一个失败用例即可阻断发布：409，is_active 保持原状。"""
    _create_prompt(client, 2, "CANDIDATE")
    failed = GateReport(
        kind="prompt",
        passed=False,
        report=EvalReport(
            results=[
                CaseResult(case_id="sec_x", name="x", category="permission_security", passed=False)
            ]
        ),
    )
    monkeypatch.setattr("bycall.admin.api._run_publish_gate", lambda kind: failed)

    resp = client.post(
        "/api/admin/prompts/2/publish", json={"canary_percent": 0, "skip_gate": False}
    )
    assert resp.status_code == 409, resp.text
    assert "发布门未通过" in resp.json()["detail"]["message"]
    assert _active_prompt_version(client) == 1  # 无副作用


def test_publish_canary_promote_rollback(client):
    """灰度发布 → 旧版保持激活；转全量 → 新版激活；回滚 → 旧版激活。"""
    _create_prompt(client, 2, "CANDIDATE")

    # 灰度 50%：is_active 仍为 v1，状态 canary。
    resp = client.post(
        "/api/admin/prompts/2/publish", json={"canary_percent": 50, "skip_gate": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["release"]["status"] == "canary"
    assert _active_prompt_version(client) == 1

    # 转全量。
    resp = client.post("/api/admin/releases/prompt/promote")
    assert resp.status_code == 200, resp.text
    assert _active_prompt_version(client) == 2

    # 一键回滚。
    resp = client.post("/api/admin/releases/prompt/rollback")
    assert resp.status_code == 200, resp.text
    assert _active_prompt_version(client) == 1

    releases = client.get("/api/admin/releases").json()
    assert releases[0]["kind"] == "prompt"
    assert releases[0]["status"] == "rolled_back"


def test_model_publish_full_and_rollback(client):
    """chat 模型全量发布 + 回滚（复用 is_active 切换）。"""

    # seed 无模型：先建 old（激活）+ new。
    resp = client.post(
        "/api/admin/models",
        json={
            "name": "chat-old",
            "display_name": "旧模型",
            "model_type": "chat",
            "provider": "generic",
            "base_url": "https://old.example/v1",
            "api_key": "sk-old",
            "is_active": True,
        },
    )
    assert resp.status_code == 201, resp.text
    old_id = resp.json()["id"]
    resp = client.post(
        "/api/admin/models",
        json={
            "name": "chat-new",
            "display_name": "新模型",
            "model_type": "chat",
            "provider": "generic",
            "base_url": "https://new.example/v1",
            "api_key": "sk-new",
            "is_active": False,
        },
    )
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["id"]

    resp = client.post(
        f"/api/admin/models/{new_id}/publish", json={"canary_percent": 0, "skip_gate": True}
    )
    assert resp.status_code == 200, resp.text
    active = next(m for m in client.get("/api/admin/models").json() if m["id"] == new_id)
    assert active["is_active"] is True

    resp = client.post("/api/admin/releases/model/rollback")
    assert resp.status_code == 200, resp.text
    old = next(m for m in client.get("/api/admin/models").json() if m["id"] == old_id)
    assert old["is_active"] is True


# ============ 目标缺失时 fail-closed（不得静默清空 is_active） ============


def test_promote_with_deleted_target_keeps_active_prompt(client):
    """灰度目标版本被删除后再转全量：409，且当前激活提示词不被清空。

    旧行为是「先全部置 False、再激活目标」——目标没了就退化成全部未激活，
    系统提示词静默变空串且返回 200（静默失效）。回归锁定 fail-closed。
    """
    _create_prompt(client, 2, "CANDIDATE")
    assert (
        client.post(
            "/api/admin/prompts/2/publish", json={"canary_percent": 50, "skip_gate": True}
        ).status_code
        == 200
    )
    assert client.delete("/api/admin/prompts/2").status_code == 204  # 灰度期间删掉候选

    resp = client.post("/api/admin/releases/prompt/promote")
    assert resp.status_code == 409, resp.text
    assert _active_prompt_version(client) == 1  # 激活版本原样保留


def test_rollback_with_deleted_previous_keeps_active_prompt(client):
    """上一版本被删除后回滚：409，仍保持当前激活版本。

    旧行为会让 ``activate_previous`` 把所有版本置 False 却无目标可激活——
    系统提示词静默变空串且返回 200。
    """
    _create_prompt(client, 2, "CANDIDATE")
    assert (
        client.post(
            "/api/admin/prompts/2/publish", json={"canary_percent": 0, "skip_gate": True}
        ).status_code
        == 200
    )
    assert _active_prompt_version(client) == 2
    assert client.delete("/api/admin/prompts/1").status_code == 204  # 删掉上一版本

    resp = client.post("/api/admin/releases/prompt/rollback")
    assert resp.status_code == 409, resp.text
    assert _active_prompt_version(client) == 2  # 没有被清空
