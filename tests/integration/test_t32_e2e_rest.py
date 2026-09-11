"""T3.2 三条平台级流程 REST 数据面端到端测试（真实连接器 + 审批链）。

与 ``test_t32_platform_flows.py``（能力集级）互补：本文件在 seed_reference 灌入的
参考场景配置上，经 ``POST /chat`` 走完整治理链路——mock LLM 只按脚本产生 tool_calls，
工具执行、审批拦截、审批执行全部走真实组件（MCP 内存通道直连 demo 连接器）。

覆盖（对齐 docs/specs/t3.2-platform-flows.md §2 + §6 验收）：
- Flow A 查询-创建闭环：guest 空手 / tech 查询+创建真实执行 / lead 可见高危
- Flow B 高风险 HITL：destructiveHint → ApprovalRequest(pending)（工具未执行）→
  admin 批准 → executed（真实生效）；tech 幻觉调用高危工具被网关拒绝（不产生审批单）
- Flow C 会话与上下文：多轮历史连续、跨会话隔离、能力披露按角色、空 RAG 管线不崩
- 安全回归（复用 T2.2 视角对参考配置）：工具越权被拦、审批绕过被拦、越权召回=0
  （召回前 ACL 过滤对参考角色 scope/level 生效）

RAG 部分按规格留占位：真实文档待用户提供，此处仅建临时 KB 验证越权召回=0。
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

# demo 连接器（examples 为参考包，平台核心不依赖；测试经内存 MCP 通道挂载）
import examples.connectors.inventory_server as inv_mod
import examples.connectors.ticket_server as tkt_mod
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from mcp.shared.memory import create_connected_server_and_client_session
from sqlmodel import create_engine, select

from bycall.api.app import create_app
from bycall.config import get_settings
from bycall.db import init_db, session_scope, set_engine
from bycall.governance.capability import clear_capability_cache
from bycall.governance.models import ApprovalRequest, User
from bycall.llm import resolver
from bycall.mcp.manager import MCPManager, reset_mcp_manager

# 连接器是进程内存态（模块级 dict），跨用例必须重置，否则演示数据互相污染。
_TICKETS_SEED = copy.deepcopy(tkt_mod._TICKETS)
_PARTS_SEED = copy.deepcopy(inv_mod._PARTS)
_NEXT_ID_SEED = tkt_mod._NEXT_ID


@pytest.fixture(autouse=True)
def _reset_demo_connector_state():
    """每个用例后还原 demo 连接器的内存数据（等价于重启连接器进程）。"""
    yield
    tkt_mod._TICKETS.clear()
    tkt_mod._TICKETS.update(copy.deepcopy(_TICKETS_SEED))
    tkt_mod._NEXT_ID = _NEXT_ID_SEED
    inv_mod._PARTS.clear()
    inv_mod._PARTS.update(copy.deepcopy(_PARTS_SEED))


def _demo_factory(config):
    """按 server 名分发到 demo 连接器的内存会话（免 stdio 子进程）。"""
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


def _scripted_llm(turns: list[AIMessage]):
    """按 invoke 次序回放脚本的 mock LLM；记录每轮 bind 的工具名与收到的 messages。

    turns 耗尽后重复最后一轮（工具回路轮数不确定时保证不 IndexError）。
    """
    m = MagicMock(spec=BaseChatModel)
    state = {"i": 0, "bound": [], "invocations": []}

    def _bind(tools, **kw):
        state["bound"] = [t.name for t in tools]
        return m

    def _invoke(messages, **kw):
        state["invocations"].append(list(messages))
        idx = min(state["i"], len(turns) - 1)
        state["i"] += 1
        return turns[idx]

    m.bind_tools.side_effect = _bind
    m.invoke.side_effect = _invoke
    m._state = state
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    """干净环境：SQLite + zvec 16 维 + dev（act-as 需 dev）。"""
    db_file = tmp_path / "test_t32_e2e.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_file}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DATA_DIR", str(tmp_path / "zvec"))
    monkeypatch.setenv("BYCALL_ENV", "dev")
    monkeypatch.setenv("BYCALL_LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False}))
    init_db()
    return tmp_path


def _load_seed():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_reference.py"
    spec = importlib.util.spec_from_file_location("seed_reference_t32", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clients(env, monkeypatch):
    """灌参考场景 + 内存 MCP 连接器 + 双 TestClient。

    - ``admin``：纯管理员 cookie（审批 / admin API）
    - ``user``：同一个 app，登录后经 ``_act_as`` 扮演 demo 账号
    """
    seed = _load_seed()
    with session_scope() as s:
        seed.seed_reference(s)

    # MCPManager 单例注入 demo 内存工厂：startup load_all 时按 DB 配置挂载。
    reset_mcp_manager()
    import bycall.mcp.manager as mgr_mod

    mgr_mod._manager = MCPManager(client_factory=_demo_factory)

    # 可切换的 mock LLM（用例内 _set_llm 换脚本）。
    box: dict = {"llm": None}
    fake_model = SimpleNamespace(id=1, name="mock-chat")
    monkeypatch.setattr(
        resolver,
        "get_chat_model_for_conversation",
        lambda session, conversation=None, **kw: (fake_model, box["llm"]),
    )

    app = create_app()
    # with 触发 startup：MCP load_all（内存通道）+ 渠道等初始化。
    with TestClient(app):
        admin = TestClient(app)
        resp = admin.post("/api/login", data={"name": "admin", "password": "admin123"})
        assert resp.status_code == 200 and resp.json().get("ok")

        user = TestClient(app)
        resp = user.post("/api/login", data={"name": "admin", "password": "admin123"})
        assert resp.status_code == 200

        yield SimpleNamespace(admin=admin, user=user, box=box)

    reset_mcp_manager()
    clear_capability_cache()


def _set_llm(clients, turns: list[AIMessage]):
    """给数据面会话换一版脚本 LLM，返回该 LLM（断言 bound/invocations 用）。"""
    llm = _scripted_llm(turns)
    clients.box["llm"] = llm
    return llm


def _act_as(clients, name: str) -> None:
    """把 user client 切换到 demo 账号（dev act-as）。"""
    with session_scope() as s:
        user = s.exec(User.select_by_name(name)).one()
        uid = user.id
    resp = clients.user.post("/api/admin/act-as", params={"target_user_id": uid})
    assert resp.status_code == 200, resp.text


def _chat(clients, question: str, conversation_id: int | None = None) -> dict:
    payload: dict = {"question": question}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    resp = clients.user.post("/chat", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tool_names(result: dict) -> list[str]:
    return [tc["name"] for tc in result.get("tool_calls", [])]


# ============ Flow A：查询-创建闭环（通用工具链）============


class TestFlowAQueryCreate:
    def test_guest_sees_no_tools_and_cannot_trigger_any(self, clients):
        """A1：guest 的 LLM 空手（bind_tools 收到空列表），绝不触发工具调用。"""
        _act_as(clients, "demo-guest")
        llm = _set_llm(clients, [AIMessage(content="我没有查询或创建记录的权限。")])

        result = _chat(clients, "你能查记录或建记录吗？")

        assert llm._state["bound"] == [], "guest 的 LLM 不应看到任何工具"
        assert _tool_names(result) == []
        assert result["decision"] == "answered"
        assert result["knowledge_scope"] == ["public"]

    def test_tech_query_ticket_executes_for_real(self, clients):
        """A2：tech 查询 T-1001 → MCP 工具真实执行 → 结果回喂 LLM。"""
        _act_as(clients, "demo-tech")
        llm = _set_llm(
            clients,
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "mcp__demo-ticket__query_ticket",
                            "args": {"ticket_id": "T-1001"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="T-1001 是一条处理中的记录。"),
            ],
        )

        result = _chat(clients, "查一下 T-1001")

        bound = llm._state["bound"]
        assert len(bound) == 5, f"tech 应见 5 个工具，实际 {bound}"
        assert "mcp__demo-ticket__close_ticket" not in bound
        assert "mcp__demo-inventory__reserve_part" not in bound
        assert _tool_names(result) == ["mcp__demo-ticket__query_ticket"]
        # 工具真实执行：tool_calls 记录里有结果内容（含种子工单标题）。
        assert "3号产线传送带异响" in json.dumps(result["tool_calls"], ensure_ascii=False)

    def test_tech_create_ticket_returns_new_id(self, clients):
        """A3：tech 创建记录 → 连接器真实落库 → 返回新 ID。"""
        _act_as(clients, "demo-tech")
        llm = _set_llm(
            clients,
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "mcp__demo-ticket__create_ticket",
                            "args": {"title": "测试-平台流程A", "priority": "normal"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="已创建。"),
            ],
        )

        result = _chat(clients, "帮我建一条标题为'测试-平台流程A'的记录")

        assert _tool_names(result) == ["mcp__demo-ticket__create_ticket"]
        assert "T-10" in json.dumps(result["tool_calls"], ensure_ascii=False)  # 新 ID ≥ T-1003
        # 工具执行结果回喂了 LLM 上下文（某轮 invocation 含 ToolMessage；
        # 末轮可能是记忆提取调用，故扫全部轮次）。
        assert any(
            getattr(m, "type", "") == "tool" for inv in llm._state["invocations"] for m in inv
        )

    def test_lead_sees_destructive_tools(self, clients):
        """A4：lead 同问可见 7 个工具（含两个高危），角色差异即治理边界。"""
        _act_as(clients, "demo-lead")
        llm = _set_llm(clients, [AIMessage(content="可以查询和创建记录。")])

        result = _chat(clients, "你能查记录吗？")

        bound = set(llm._state["bound"])
        assert len(bound) == 7
        assert {"mcp__demo-ticket__close_ticket", "mcp__demo-inventory__reserve_part"} <= bound
        assert _tool_names(result) == []


# ============ Flow B：高风险操作 HITL 审批门 ============


class TestFlowBApprovalGate:
    def test_lead_close_ticket_blocked_then_executed_after_approval(self, clients):
        """B1→B2：destructiveHint 拦截生成 pending 审批（工单未动）；批准后真实执行。"""
        _act_as(clients, "demo-lead")
        _set_llm(
            clients,
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "mcp__demo-ticket__close_ticket",
                            "args": {"ticket_id": "T-1001", "resolution": "已更换轴承"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="该操作为高风险操作，已提交审批。"),
            ],
        )

        _chat(clients, "关闭 T-1001")

        # 审批单生成，工单未被关闭（审批前不得执行）。
        with session_scope() as s:
            reqs = s.exec(
                select(ApprovalRequest).where(
                    ApprovalRequest.tool_name == "mcp__demo-ticket__close_ticket",
                    ApprovalRequest.status == "pending",
                )
            ).all()
            assert len(reqs) == 1
            rid = reqs[0].id
        from examples.connectors.ticket_server import _TICKETS

        assert _TICKETS["T-1001"]["status"] == "processing", "审批前工单不得被关闭"

        # admin 批准（once 档）→ 工具真实执行 → 状态 executed。
        resp = clients.admin.post(f"/api/admin/approvals/{rid}/approve", json={"note": "同意"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "executed"
        assert _TICKETS["T-1001"]["status"] == "closed", "批准后 close_ticket 应真实生效"

        # 审批执行也进台账（ApprovalGrant 由 T3.3 覆盖，此处锁端到端串起来）。
        with session_scope() as s:
            req = s.get(ApprovalRequest, rid)
            assert req.status == "executed"

    def test_tech_hallucinated_destructive_call_rejected(self, clients):
        """安全回归·工具越权：tech 白名单无 close_ticket，LLM 幻觉调用被网关拒绝。

        拒绝发生在审批门之前（工具根本不在 LLM 可见集合），不得产生审批单、
        不得执行（纵深：即使审批通过队列里有脏单也执行不了，因为压根没有单）。
        """
        _act_as(clients, "demo-tech")
        llm = _set_llm(
            clients,
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "mcp__demo-ticket__close_ticket",
                            "args": {"ticket_id": "T-1001", "resolution": "x"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="我没有关闭记录的权限。"),
            ],
        )

        _chat(clients, "关闭 T-1001")

        # 工具未执行：T-1001 仍 processing；无审批单产生。
        from examples.connectors.ticket_server import _TICKETS

        assert _TICKETS["T-1001"]["status"] == "processing"
        with session_scope() as s:
            pending = s.exec(
                select(ApprovalRequest).where(ApprovalRequest.status == "pending")
            ).all()
        assert pending == [], "越权工具调用不得进入审批队列"
        # LLM 收到的工具列表里没有 close_ticket（幻觉调用必然走 unknown_tool 容错）。
        assert "mcp__demo-ticket__close_ticket" not in llm._state["bound"]


# ============ Flow C：多轮上下文与能力边界感知 ============


class TestFlowCContextAndBoundary:
    def test_multi_turn_history_loaded(self, clients):
        """C1：同会话第二轮请求的历史里含第一轮问答。"""
        _act_as(clients, "demo-tech")
        _set_llm(clients, [AIMessage(content="你是 demo-tech。")])
        cid = _chat(clients, "我是谁？")["conversation_id"]

        llm2 = _set_llm(clients, [AIMessage(content="基于上文回答。")])
        _chat(clients, "刚才我说了什么？", conversation_id=cid)

        second_messages = llm2._state["invocations"][-1]
        contents = [m.content for m in second_messages if getattr(m, "type", "") == "human"]
        assert "我是谁？" in contents, "第二轮必须能看到第一轮的用户消息"

    def test_new_conversation_isolated(self, clients):
        """C3：新会话看不到旧会话上下文。"""
        _act_as(clients, "demo-tech")
        _set_llm(clients, [AIMessage(content="好。")])
        first_cid = _chat(clients, "第一会话的暗号是 X7。")["conversation_id"]

        llm2 = _set_llm(clients, [AIMessage(content="不知道。")])
        second = _chat(clients, "暗号是什么？")

        assert second["conversation_id"] != first_cid
        contents = [m.content for m in llm2._state["invocations"][-1]]
        assert not any("X7" in c for c in contents if isinstance(c, str)), (
            "新会话不得看到旧会话内容"
        )

    def test_capability_disclosure_differs_by_role(self, clients):
        """C2：同问不同角色，Runtime Context 披露的工具集不同（tech 无高危 / lead 有）。"""
        _act_as(clients, "demo-tech")
        llm_tech = _set_llm(clients, [AIMessage(content="我不能关闭记录。")])
        _chat(clients, "你能关闭记录吗？")
        tech_system = llm_tech._state["invocations"][-1][0].content

        _act_as(clients, "demo-lead")
        llm_lead = _set_llm(clients, [AIMessage(content="我可以关闭记录。")])
        _chat(clients, "你能关闭记录吗？")
        lead_system = llm_lead._state["invocations"][-1][0].content

        assert "close_ticket" not in tech_system, "tech 的能力披露不得含高危工具"
        assert "close_ticket" in lead_system, "lead 的能力披露应含高危工具"

    def test_empty_rag_pipeline_no_crash(self, clients):
        """C4：无 KB 时检索返回空、管线不崩、审计带 context_trace。"""
        _act_as(clients, "demo-tech")
        _set_llm(clients, [AIMessage(content="知识库暂无相关内容。")])

        result = _chat(clients, "平台的部署规范是什么？")

        assert result["decision"] == "answered"
        assert result["retrieved_chunks"] == 0
        assert result["context_trace"] != "", "空库也要留上下文预算审计痕迹"


# ============ 安全回归：越权召回 = 0（参考角色 scope/level 配置）============


class TestSecurityUnauthorizedRecall:
    @pytest.fixture
    def kb_store(self, env):
        """临时 KB：真实 zvec + 2 篇文档（public 普通级 / ops 机密级 level=2）。

        按规格 RAG 留占位——不入 seed_reference（避免 embedding 模型依赖），
        仅在此验证参考角色 scope/level 配置下「召回前过滤」生效。
        """
        from bycall.evals.harness import zero_embed_model
        from bycall.rag.ingest import IngestPipeline
        from bycall.vector_store.factory import create_vector_store

        store = create_vector_store(get_settings().vector_store)
        embed = zero_embed_model(dimension=get_settings().vector_store.dimension)
        pipe = IngestPipeline(store, embed)
        pipe.ingest_text("公开：平台部署手册说明如何启动服务。", source="public", doc_id="pub_doc")
        pipe.ingest_text(
            "机密：ops 数据库root口令与生产密钥清单。", source="ops", doc_id="sec_doc", level=2
        )
        yield store
        store.close()

    def test_guest_cannot_recall_ops_secret(self, kb_store):
        """guest（scope=[public], level=0）：机密文档在召回前即被过滤（越权召回=0）。"""
        from bycall.rag.retrieve import retrieve_context

        hits = retrieve_context(
            kb_store,
            "数据库口令 密钥",
            knowledge_scope_tags=["public"],
            level=0,
        )
        assert all(h.id != "sec_doc" for h in hits), "越权召回 > 0：机密文档出现在 guest 召回结果"
        assert all(h.fields.get("source") == "public" for h in hits)

    def test_tech_sees_public_but_not_level2_secret(self, kb_store):
        """tech（scope=[public,ops], level=1）：可见 public，仍不可见 level=2 机密。"""
        from bycall.rag.retrieve import retrieve_context

        hits = retrieve_context(
            kb_store,
            "部署手册",
            knowledge_scope_tags=["public", "ops"],
            level=1,
        )
        assert any(h.id == "pub_doc" or h.fields.get("source") == "public" for h in hits)

        hits2 = retrieve_context(
            kb_store,
            "数据库口令 密钥",
            knowledge_scope_tags=["public", "ops"],
            level=1,
        )
        assert all(h.id != "sec_doc" for h in hits2), "低职级召回了机密文档"
