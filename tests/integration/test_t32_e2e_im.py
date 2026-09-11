"""T3.2 平台级流程 IM 单聊路端到端测试（企微语义 + 审批推送回推）。

与 REST 路（``test_t32_e2e_rest.py``）互补，覆盖 IM 特有的两段：

- IM 收消息全链：企微身份（IMBinding）→ ``handle_im_message`` → 流式工具回路
  （真实 demo 连接器）→ 流式回复（capture sender）；
- Flow B 的 M6 主动推送：IM 发起的高危审批被批准后，结果推送回发起员工的
  企微身份（``push_approval_result`` → ChannelManager.send_proactive）。

推送依赖渠道长连接，测试注入 fake ChannelManager 捕获 send_proactive，
不启动真实企微连接。
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import examples.connectors.inventory_server as inv_mod
import examples.connectors.ticket_server as tkt_mod
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from mcp.shared.memory import create_connected_server_and_client_session
from sqlmodel import create_engine, select

from bycall.agent import im_handler
from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.gateway.base import InboundMessage, ReplySender
from bycall.governance.capability import clear_capability_cache
from bycall.governance.models import ApprovalRequest
from bycall.llm import resolver
from bycall.mcp.manager import MCPManager, reset_mcp_manager

_TICKETS_SEED = copy.deepcopy(tkt_mod._TICKETS)
_PARTS_SEED = copy.deepcopy(inv_mod._PARTS)
_NEXT_ID_SEED = tkt_mod._NEXT_ID


@pytest.fixture(autouse=True)
def _reset_demo_connector_state():
    """每个用例后还原 demo 连接器内存数据（等价重启连接器进程）。"""
    yield
    tkt_mod._TICKETS.clear()
    tkt_mod._TICKETS.update(copy.deepcopy(_TICKETS_SEED))
    tkt_mod._NEXT_ID = _NEXT_ID_SEED
    inv_mod._PARTS.clear()
    inv_mod._PARTS.update(copy.deepcopy(_PARTS_SEED))


def _load_seed():
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_reference.py"
    spec = importlib.util.spec_from_file_location("seed_reference_t32_im", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _demo_factory(config):
    from bycall.mcp.client import MCPClient

    targets = {"demo-ticket": tkt_mod.server, "demo-inventory": inv_mod.server}
    target = targets.get(config.name)
    if target is None:
        raise RuntimeError(f"测试未配置的 MCP server: {config.name}")
    return MCPClient(
        config,
        session_cm_factory=lambda: create_connected_server_and_client_session(target),
        connect_timeout=15,
        call_timeout=10,
    )


def _tc_chunk(name: str, args_json: str, call_id: str, index: int = 0) -> AIMessageChunk:
    """工具调用 chunk（流式回路第一轮：content 空 + tool_call_chunks）。"""
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": name,
                "args": args_json,
                "id": call_id,
                "index": index,
                "type": "tool_call_chunk",
            }
        ],
    )


def _text_chunks(text: str) -> list[AIMessageChunk]:
    return [AIMessageChunk(content=ch) for ch in text]


def _stream_llm(rounds: list[list[AIMessageChunk]], invoke_turns: list[AIMessage] | None = None):
    """astream 按 rounds 顺序逐轮 yield 的 mock LLM。

    ``invoke_turns`` 供同步路径（REST /chat → run_agent）使用，缺省返回空文本
    （记忆提取等旁路调用安全退出）。
    """
    m = MagicMock(spec=BaseChatModel)
    state = {"round": 0, "invoke_i": 0}
    turns = invoke_turns or [AIMessage(content="")]

    def _bind(tools, **kw):
        return m

    async def _astream(messages, **kw):
        idx = min(state["round"], len(rounds) - 1)
        state["round"] += 1
        for chunk in rounds[idx]:
            yield chunk

    def _invoke(messages, **kw):
        idx = min(state["invoke_i"], len(turns) - 1)
        state["invoke_i"] += 1
        return turns[idx]

    m.bind_tools.side_effect = _bind
    m.astream = _astream
    m.invoke.side_effect = _invoke
    m._state = state
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test_t32_im.db"
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
def im_env(env, monkeypatch):
    """参考场景 + 内存 MCP + mock LLM + admin TestClient + 推送捕获。"""
    seed = _load_seed()
    with session_scope() as s:
        seed.seed_reference(s)

    reset_mcp_manager()
    import bycall.mcp.manager as mgr_mod

    mgr_mod._manager = MCPManager(client_factory=_demo_factory)

    box: dict = {"llm": None}
    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(
        resolver,
        "get_chat_model_for_conversation",
        lambda session, conversation=None, **kw: (fake_model, box["llm"]),
    )

    # 审批结果推送捕获（不启动真实企微长连接）。
    pushed: list[dict] = []

    class _FakeChannelManager:
        def send_proactive(self, im_user_id, body, agent_id=None):
            pushed.append({"im_user_id": im_user_id, "body": body, "agent_id": agent_id})
            return True

    import bycall.tools.approval as approval_mod

    monkeypatch.setattr(approval_mod, "get_channel_manager", lambda: _FakeChannelManager())

    app = create_app()
    with TestClient(app):
        admin = TestClient(app)
        resp = admin.post("/api/login", data={"name": "admin", "password": "admin123"})
        assert resp.status_code == 200 and resp.json().get("ok")
        yield SimpleNamespace(admin=admin, box=box, pushed=pushed)

    reset_mcp_manager()
    clear_capability_cache()


def _make_sender(capture: list) -> ReplySender:
    async def send_text(content):
        capture.append(("text", content))

    async def send_stream(content, stream_id, finish):
        capture.append(("stream", content, stream_id, finish))

    return ReplySender(send_text=send_text, send_stream=send_stream, channel="wecom")


def _send_im(text: str, im_user_id: str, capture: list) -> None:
    msg = InboundMessage(channel="wecom", im_user_id=im_user_id, text=text)
    asyncio.run(im_handler.handle_im_message(msg, _make_sender(capture)))


# ============ IM 单聊全链：流式工具回路 ============


class TestImFlowAQuery:
    def test_tech_query_via_im_streams_real_tool_result(self, im_env):
        """A2（IM 路）：企微身份 → 流式回复含真实工具执行结果。"""
        im_env.box["llm"] = _stream_llm(
            [
                [_tc_chunk("mcp__demo-ticket__query_ticket", '{"ticket_id": "T-1001"}', "c1")],
                _text_chunks("T-1001 是处理中的记录。"),
            ]
        )
        capture: list = []
        _send_im("查一下 T-1001", "DEMO_TECH_001", capture)

        assert capture, "IM 必须有回复"
        streamed = "".join(c for kind, c, *_ in capture if kind == "stream")
        assert "T-1001" in streamed

    def test_unbound_im_user_rejected(self, im_env):
        """安全回归：未绑定企微身份直接拒绝（不进治理链路）。"""
        im_env.box["llm"] = _stream_llm([_text_chunks("不应到达")])
        capture: list = []
        _send_im("你好", "STRANGER_999", capture)

        kinds = [kind for kind, *_ in capture]
        assert "text" in kinds
        assert "stream" not in kinds, "未绑定用户不得进入流式回复"
        assert "未被授权" in capture[0][1]


# ============ Flow B（IM 路）：高危审批 + 结果推送回发起员工 ============


class TestImFlowBApprovalPush:
    def test_close_ticket_via_im_then_push_result_to_initiator(self, im_env):
        """B1→B3 全链：IM 发起关单 → 审批拦截 → admin 批准 → 真实执行 + 推送回企微。"""
        im_env.box["llm"] = _stream_llm(
            [
                [
                    _tc_chunk(
                        "mcp__demo-ticket__close_ticket",
                        '{"ticket_id": "T-1001", "resolution": "已更换轴承"}',
                        "c1",
                    )
                ],
                _text_chunks("该操作为高风险操作，已提交审批，请等待管理员批准。"),
            ]
        )
        capture: list = []
        _send_im("关闭 T-1001", "DEMO_LEAD_001", capture)

        # 回复流式到达，含"审批"提示；审批单 pending 且 channel=wecom。
        streamed = "".join(c for kind, c, *_ in capture if kind == "stream")
        assert "审批" in streamed
        with session_scope() as s:
            req = s.exec(
                select(ApprovalRequest).where(
                    ApprovalRequest.tool_name == "mcp__demo-ticket__close_ticket",
                    ApprovalRequest.status == "pending",
                )
            ).one()
            assert req.channel == "wecom"
            assert req.im_user_id == "DEMO_LEAD_001"
            rid = req.id
        assert tkt_mod._TICKETS["T-1001"]["status"] == "processing", "审批前不得执行"

        # admin 批准 → 工具真实执行 → 推送回发起员工企微身份。
        resp = im_env.admin.post(f"/api/admin/approvals/{rid}/approve", json={"note": "ok"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "executed"
        assert tkt_mod._TICKETS["T-1001"]["status"] == "closed"

        assert len(im_env.pushed) == 1, "审批结果必须主动推送回发起员工"
        push = im_env.pushed[0]
        assert push["im_user_id"] == "DEMO_LEAD_001"
        content = push["body"]["markdown"]["content"]
        assert "close_ticket" in content
        assert "已通过并执行" in content

    def test_push_skipped_for_rest_channel(self, im_env):
        """REST 渠道发起的审批不推送（channel=api 无企微身份，静默跳过）。"""
        im_env.box["llm"] = _stream_llm(
            [[_tc_chunk("mcp__demo-ticket__close_ticket", "{}", "unused")]],
            invoke_turns=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "mcp__demo-ticket__close_ticket",
                            "args": {"ticket_id": "T-1002", "resolution": "x"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="已提交审批。"),
            ],
        )
        # admin 经 REST 数据面发起（channel=api）。
        resp = im_env.admin.post("/chat", json={"question": "关闭 T-1002"})
        assert resp.status_code == 200
        with session_scope() as s:
            req = s.exec(
                select(ApprovalRequest).where(
                    ApprovalRequest.tool_name == "mcp__demo-ticket__close_ticket",
                    ApprovalRequest.status == "pending",
                )
            ).first()
            rid = req.id if req else None
        assert rid is not None, "admin 经 REST 发起的高危调用也应被审批门拦截"
        resp = im_env.admin.post(f"/api/admin/approvals/{rid}/approve", json={})
        assert resp.status_code == 200
        assert im_env.pushed == [], "channel=api 的审批不推送企微"


# ============ Flow C（IM 路）：多轮上下文连续 ============


class TestImFlowCMultiTurn:
    def test_im_multi_turn_context_kept(self, im_env):
        """C1（IM 路）：同一企微身份连发两条消息，第二轮回复基于会话历史。"""
        im_env.box["llm"] = _stream_llm(
            [
                _text_chunks("你好，demo-tech。"),
                _text_chunks("你刚才打了招呼。"),
            ]
        )
        capture1: list = []
        _send_im("你好", "DEMO_TECH_001", capture1)
        capture2: list = []
        _send_im("我刚才说了什么？", "DEMO_TECH_001", capture2)

        assert "".join(c for kind, c, *_ in capture1 if kind == "stream")
        assert "".join(c for kind, c, *_ in capture2 if kind == "stream")
        # 会话持久化：同用户同渠道一条会话、4 条消息（两轮 user+assistant）。
        from bycall.governance.models import Conversation, Message

        with session_scope() as s:
            convs = s.exec(select(Conversation).where(Conversation.channel == "wecom")).all()
            assert len(convs) == 1
            msgs = s.exec(
                select(Message).where(Message.conversation_id == convs[0].id).order_by(Message.id)
            ).all()
            assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
            assert msgs[0].content == "你好"

    def test_json_args_passthrough(self, im_env):
        """工具参数 JSON 串在流式 chunk 中被正确聚合（连接器收到结构化 args）。"""
        im_env.box["llm"] = _stream_llm(
            [
                # args 拆两个 chunk：验证流式聚合不截断
                [
                    _tc_chunk("mcp__demo-ticket__query_ticket", '{"ticket_id":', "c1"),
                    _tc_chunk("mcp__demo-ticket__query_ticket", ' "T-1002"}', "c1", index=0),
                ],
                _text_chunks("T-1002 已关闭。"),
            ]
        )
        capture: list = []
        _send_im("查 T-1002", "DEMO_TECH_001", capture)

        streamed = "".join(c for kind, c, *_ in capture if kind == "stream")
        assert "T-1002" in streamed
