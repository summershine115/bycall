"""M6 审批结果主动推送单元测试（P0 criterion）。

验证 ``_push_approval_result`` + ``execute_approved_tool`` 的推送钩子：
- 企微渠道且有 im_user_id → 推送一次（chatid=im_user_id，markdown 含结果）
- 渠道非企微 / 无 im_user_id → 不推送
- 推送失败（异常 / 返回 False）→ 不影响审批执行结果
- 建单时未捕获 im_user_id 但 IM 绑定存在 → 反查成功推送
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlmodel import create_engine

import bycall.tools.approval as approval_mod
from bycall.config import get_settings
from bycall.tools.approval import execute_approved_tool, push_approval_result
from bycall.tools.base import ToolSpec
from bycall.tools.registry import register


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """临时 SQLite DB + init_db。不持有 session——按需用 session_scope()。"""
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(
        create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    )
    init_db()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_manager(monkeypatch):
    """替换 bycall.tools.approval.get_channel_manager，返回可控的 mock。"""
    mgr = MagicMock()
    mgr.send_proactive.return_value = True
    monkeypatch.setattr(approval_mod, "get_channel_manager", lambda: mgr)
    return mgr


def _req(**kw):
    """构造一个具备推送所需属性的假审批请求对象。"""
    defaults = {
        "id": 42,
        "tool_name": "danger_delete",
        "channel": "wecom",
        "im_user_id": "WecomUid001",
        "user_id": 7,
        "user_name": "alice",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _register_tool(name: str):
    """注册一个返回固定串的测试工具。"""

    def _func(**kwargs):
        return f"deleted:{kwargs}"

    register(
        ToolSpec(
            name=name,
            description="删除操作",
            parameters={"type": "object", "properties": {}},
            func=_func,
        )
    )


def _create_user(name: str, role: str = "admin", is_active: bool = True) -> int:
    from bycall.db import session_scope
    from bycall.governance.models import User

    with session_scope() as s:
        u = User(name=name, display_name=name, role=role, is_active=is_active)
        s.add(u)
        s.flush()
        uid = u.id
    assert uid is not None
    return uid


def _bind_wecom(user_id: int, im_user_id: str) -> None:
    from bycall.db import session_scope
    from bycall.governance.models import IMBinding

    with session_scope() as session:
        session.add(IMBinding(user_id=user_id, channel="wecom", im_user_id=im_user_id))


# ============ _push_approval_result 直接单测 ============


class TestPushApprovalResult:
    def test_push_uses_matching_current_binding(self, engine, fake_manager):
        """发起身份仍是当前有效绑定时推送脱敏后的执行结果。"""
        user_id = _create_user("matching_user")
        _bind_wecom(user_id, "WecomUid001")

        push_approval_result(_req(user_id=user_id), "已删除记录 rec_99")

        fake_manager.send_proactive.assert_called_once()
        chatid, body = fake_manager.send_proactive.call_args.args
        assert chatid == "WecomUid001"
        assert body["msgtype"] == "markdown"
        assert "已删除记录 rec_99" in body["markdown"]["content"]

    def test_legacy_request_uses_current_binding(self, engine, fake_manager):
        """旧请求未捕获身份时，可使用同一用户当前有效的企微绑定。"""
        user_id = _create_user("legacy_user")
        _bind_wecom(user_id, "WecomLegacy")

        push_approval_result(_req(user_id=user_id, im_user_id=""), "done")

        fake_manager.send_proactive.assert_called_once()
        assert fake_manager.send_proactive.call_args.args[0] == "WecomLegacy"

    def test_changed_binding_is_not_notified(self, engine, fake_manager):
        """审批等待期间绑定变化时拒绝推送，避免泄漏给新旧任一身份。"""
        user_id = _create_user("changed_user")
        _bind_wecom(user_id, "WecomCurrent")

        push_approval_result(_req(user_id=user_id, im_user_id="WecomCaptured"), "secret")

        fake_manager.send_proactive.assert_not_called()

    def test_inactive_user_is_not_notified(self, engine, fake_manager):
        """用户在审批期间被禁用后不能再收到结果。"""
        user_id = _create_user("disabled_user", is_active=False)
        _bind_wecom(user_id, "WecomDisabled")

        push_approval_result(_req(user_id=user_id, im_user_id="WecomDisabled"), "secret")

        fake_manager.send_proactive.assert_not_called()

    def test_non_wecom_channel_is_not_notified(self, engine, fake_manager):
        user_id = _create_user("feishu_user")
        push_approval_result(_req(user_id=user_id, channel="feishu"), "ok")
        fake_manager.send_proactive.assert_not_called()

    def test_push_failure_is_swallowed(self, engine, fake_manager):
        user_id = _create_user("push_failure_user")
        _bind_wecom(user_id, "WecomFailure")
        fake_manager.send_proactive.side_effect = RuntimeError("boom")

        push_approval_result(_req(user_id=user_id, im_user_id="WecomFailure"), "ok")


# ============ execute_approved_tool 集成（推送钩子接入执行路径）============


class TestExecuteApprovedToolPush:
    def test_execute_success_does_not_push_before_commit(self, engine, monkeypatch):
        """执行函数只返回结果，通知由提交事务后的 admin 端点负责。"""
        _register_tool("push_safe_delete")
        from bycall.db import session_scope
        from bycall.governance.models import ApprovalRequest

        user_id = _create_user("push_admin")
        with session_scope() as session:
            req = ApprovalRequest(
                user_id=user_id,
                user_name="push_admin",
                tool_name="push_safe_delete",
                tool_args_json="{}",
                status="pending",
                channel="wecom",
                im_user_id="WecomPush",
            )
            session.add(req)
            session.flush()
            request_id = req.id

        with session_scope() as session:
            req_obj = session.get(ApprovalRequest, request_id)
            session.expunge(req_obj)

        manager = MagicMock()
        monkeypatch.setattr(approval_mod, "get_channel_manager", lambda: manager)

        ok, result = execute_approved_tool(req_obj)

        assert ok is True
        assert "deleted:" in result
        manager.send_proactive.assert_not_called()


class TestApproveEndpointPushOrdering:
    def test_endpoint_commits_before_push(self, monkeypatch):
        """状态事务成功提交后，端点才触发主动通知。"""
        from bycall.admin import api as admin_api

        req = _req(status="pending", conversation_id=None)
        session = MagicMock()
        events: list[str] = []
        session.commit.side_effect = lambda: events.append("commit")
        monkeypatch.setattr(admin_api, "_get_approval_or_404", lambda *_: req)
        monkeypatch.setattr(admin_api, "_get_operator_name", lambda *_: "admin")
        monkeypatch.setattr(admin_api, "_audit_admin", lambda *_: None)
        monkeypatch.setattr(admin_api, "_approval_to_out", lambda value: value)
        monkeypatch.setattr(approval_mod, "execute_approved_tool", lambda _: (True, "done"))
        monkeypatch.setattr(
            approval_mod,
            "push_approval_result",
            lambda *_: events.append("push"),
        )

        result = admin_api.approve_approval(
            42,
            SimpleNamespace(note="ok"),
            MagicMock(),
            session,
            None,
        )

        assert events == ["commit", "push"]
        assert result.status == "executed"

    def test_commit_failure_never_pushes(self, monkeypatch):
        """审批事务未落库时不能向员工发送成功通知。"""
        from bycall.admin import api as admin_api

        req = _req(status="pending", conversation_id=None)
        session = MagicMock()
        session.commit.side_effect = RuntimeError("commit failed")
        push = MagicMock()
        monkeypatch.setattr(admin_api, "_get_approval_or_404", lambda *_: req)
        monkeypatch.setattr(admin_api, "_get_operator_name", lambda *_: "admin")
        monkeypatch.setattr(admin_api, "_audit_admin", lambda *_: None)
        monkeypatch.setattr(approval_mod, "execute_approved_tool", lambda _: (True, "done"))
        monkeypatch.setattr(approval_mod, "push_approval_result", push)

        with pytest.raises(RuntimeError, match="commit failed"):
            admin_api.approve_approval(
                42,
                SimpleNamespace(note="ok"),
                MagicMock(),
                session,
                None,
            )

        push.assert_not_called()
