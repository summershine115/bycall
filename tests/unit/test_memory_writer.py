"""agent 可写记忆路径测试（S2 Phase 2）。

覆盖四类真实 bug：
- **闸门**：低置信 / volatile / 非法 JSON / 非法 category → 正确跳过；高置信持久事实才写。
- **去重 + admin 权威**：同事实重复轮不双写；admin 已有同事实 → agent 不覆盖；归一化匹配。
- **信任（红线 #7）**：写入的记忆 source=agent，召回时进 **user 角色**（不进 system）。
- **触发**：answered+工具调用轮触发提取；纯闲聊（无工具）不触发（成本约束）。
- **superseded 联动**：agent 记忆被 admin 标 superseded 后不再注入。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, create_engine

from bycall.agent.graph import GraphResult
from bycall.agent.memory_writer import (
    _ExtractSettings,
    _gate,
    _normalize,
    _parse_json_facts,
    maybe_extract_and_save_memory,
)
from bycall.agent.runner import run_agent_stream
from bycall.agent.session_store import build_conversation_messages, load_agent_memory
from bycall.db import init_db, set_engine
from bycall.governance.models import User, UserMemory


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    """干净 SQLite + init_db（建表 + 种子，含 memory.* 设置）。"""
    db_path = tmp_path / "test_memwrite.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    return Session(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))


@pytest.fixture
def user_id(db: Session) -> int:
    """建一个真实用户（记忆按 user_id 隔离，用真实 id 更贴近生产行为）。"""
    u = User(name="memtest", display_name="Mem", role="member", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u.id


class _FakeLLM:
    """桩 LLM：invoke 返回固定文本，记录调用次数。"""

    def __init__(self, response: str):
        self.response = response
        self.invoke_count = 0

    def invoke(self, messages):
        self.invoke_count += 1
        return SimpleNamespace(content=self.response)


def _agent_memories(db: Session, user_id: int) -> list[UserMemory]:
    return list(db.exec(UserMemory.select_by_user(user_id)).all())


# ============ 闸门：高置信持久事实才写 ============


class TestGate:
    def test_high_confidence_persistent_fact_written(self, db: Session, user_id: int):
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "负责华东区销售",
                        "category": "fact",
                        "confidence": 0.9,
                        "volatile": False,
                    }
                ]
            )
        )
        n = maybe_extract_and_save_memory(
            llm=llm, user_id=user_id, question="我是华东区销售", answer="好的，已记录。"
        )
        assert n == 1
        mems = _agent_memories(db, user_id)
        assert len(mems) == 1
        m = mems[0]
        assert m.source == "agent"
        assert m.status == "active"
        assert m.confidence == 0.9
        assert m.category == "fact"
        assert m.content == "负责华东区销售"

    def test_low_confidence_skipped(self, db: Session, user_id: int):
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "可能喜欢咖啡",
                        "category": "preference",
                        "confidence": 0.5,
                        "volatile": False,
                    }
                ]
            )
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )
        assert _agent_memories(db, user_id) == []

    def test_volatile_skipped(self, db: Session, user_id: int):
        """时间敏感信息（价格/临时状态）即使高置信也不写。"""
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "今天股价 12.5",
                        "category": "fact",
                        "confidence": 0.95,
                        "volatile": True,
                    }
                ]
            )
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )
        assert _agent_memories(db, user_id) == []

    def test_invalid_category_skipped(self, db: Session, user_id: int):
        llm = _FakeLLM(
            json.dumps([{"fact": "xx", "category": "secret", "confidence": 0.9, "volatile": False}])
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )

    def test_invalid_json_silently_skipped(self, db: Session, user_id: int):
        """LLM 返回非 JSON 散文 → 静默跳过，不抛、不写。"""
        llm = _FakeLLM("我觉得用户可能是销售，但不太确定。")
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )
        assert _agent_memories(db, user_id) == []

    def test_json_fenced_output_parsed(self):
        """LLM 把 JSON 包在 ```json 围栏里也能解析。"""
        text = '```json\n[{"fact":"偏好中文","category":"preference","confidence":0.9,"volatile":false}]\n```'
        facts = _parse_json_facts(text)
        assert len(facts) == 1
        assert facts[0]["fact"] == "偏好中文"

    def test_empty_array_no_write(self, db: Session, user_id: int):
        assert (
            maybe_extract_and_save_memory(
                llm=_FakeLLM("[]"), user_id=user_id, question="x", answer="y"
            )
            == 0
        )

    def test_mixed_candidates_partial_write(self, db: Session, user_id: int):
        """一次提取多条候选，闸门只放行合格的——防「全写或全不写」的粗放逻辑。"""
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "偏好邮件沟通",
                        "category": "preference",
                        "confidence": 0.9,
                        "volatile": False,
                    },
                    {"fact": "低置信项", "category": "fact", "confidence": 0.3, "volatile": False},
                    {"fact": "今日特价", "category": "fact", "confidence": 0.95, "volatile": True},
                    {"fact": "负责财务", "category": "fact", "confidence": 0.88, "volatile": False},
                ]
            )
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 2
        )
        contents = {m.content for m in _agent_memories(db, user_id)}
        assert contents == {"偏好邮件沟通", "负责财务"}

    def test_disabled_setting_skips_extraction(self, db: Session, user_id: int):
        """SystemSetting.memory.agent_extract_enabled=false → 不提取（连 LLM 都不调）。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "memory.agent_extract_enabled", "false")
        llm = _FakeLLM(
            json.dumps([{"fact": "x", "category": "fact", "confidence": 0.99, "volatile": False}])
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )
        assert llm.invoke_count == 0  # 关闭时根本不调 LLM

    def test_threshold_respects_systemsetting(self, db: Session, user_id: int):
        """阈值可经 SystemSetting 调高——0.9 的候选在阈值 0.95 下被拒。"""
        from bycall.db import session_scope
        from bycall.governance.settings_store import set_setting_value

        with session_scope() as s:
            set_setting_value(s, "memory.agent_confidence_threshold", "0.95")
        llm = _FakeLLM(
            json.dumps(
                [{"fact": "负责运营", "category": "fact", "confidence": 0.9, "volatile": False}]
            )
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )


# ============ 去重 + admin 权威 ============


class TestDedup:
    def test_same_fact_repeat_turn_not_double_written(self, db: Session, user_id: int):
        """两轮提取出同一事实 → 只写一条（防重复轮双写膨胀）。"""
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "负责华东区销售",
                        "category": "fact",
                        "confidence": 0.9,
                        "volatile": False,
                    }
                ]
            )
        )
        maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="q1", answer="a1")
        maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="q2", answer="a2")
        assert len(_agent_memories(db, user_id)) == 1

    def test_admin_memory_blocks_agent_write(self, db: Session, user_id: int):
        """已有 admin 记忆覆盖同一事实 → agent 不覆盖（admin 是权威）。"""
        db.add(
            UserMemory(
                user_id=user_id,
                category="fact",
                content="负责华东区销售",
                source="admin",
                confidence=1.0,
                status="active",
            )
        )
        db.commit()
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "负责华东区销售",
                        "category": "fact",
                        "confidence": 0.9,
                        "volatile": False,
                    }
                ]
            )
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )
        # 仍只有那条 admin 记忆，没有被 agent 覆盖/新增
        mems = _agent_memories(db, user_id)
        assert len(mems) == 1
        assert mems[0].source == "admin"

    def test_superseded_agent_memory_not_revived(self, db: Session, user_id: int):
        """已被 admin 标 superseded 的 agent 记忆，重提取同事实不复活（尊重纠正）。"""
        db.add(
            UserMemory(
                user_id=user_id,
                category="fact",
                content="负责华东区销售",
                source="agent",
                confidence=0.9,
                status="superseded",
            )
        )
        db.commit()
        llm = _FakeLLM(
            json.dumps(
                [
                    {
                        "fact": "负责华东区销售",
                        "category": "fact",
                        "confidence": 0.95,
                        "volatile": False,
                    }
                ]
            )
        )
        assert (
            maybe_extract_and_save_memory(llm=llm, user_id=user_id, question="x", answer="y") == 0
        )
        mems = _agent_memories(db, user_id)
        assert len(mems) == 1
        assert mems[0].status == "superseded"  # 没有新增 active 的

    def test_normalization_matches_case_and_whitespace(self):
        """归一化消除大小写/空白差异——防「换个空格就当成新事实」双写。"""
        assert _normalize("负责  华东\t区销售") == _normalize("负责华东区销售")
        assert _normalize("Like Coffee") == _normalize("like   coffee")


# ============ 信任（红线 #7）：agent 记忆进 user 角色 ============


class TestTrustIsolation:
    def test_written_memory_injected_in_user_role_not_system(self, db: Session, user_id: int):
        """写入的 agent 记忆经 load_agent_memory 召回，注入 user 角色（绝不进 system）。"""
        maybe_extract_and_save_memory(
            llm=_FakeLLM(
                json.dumps(
                    [
                        {
                            "fact": "偏好中文回复",
                            "category": "preference",
                            "confidence": 0.9,
                            "volatile": False,
                        }
                    ]
                )
            ),
            user_id=user_id,
            question="x",
            answer="y",
        )
        from bycall.db import session_scope

        with session_scope() as s:
            agent_mems = load_agent_memory(s, user_id)
            assert len(agent_mems) == 1
            assert agent_mems[0].source == "agent"
            messages = build_conversation_messages(
                system_prompt="你是数字员工。",
                rag_context="",
                question="你好",
                history=[],
                summary="",
                agent_memories=agent_mems,
            )
        roles = [(m.role, m.content) for m in messages]
        system_text = next(c for r, c in roles if r == "system")
        user_texts = [c for r, c in roles if r == "user"]
        # agent 记忆出现在某条 user 消息里（<agent_memory> 块），绝不在 system
        assert any("<agent_memory>" in t and "偏好中文回复" in t for t in user_texts)
        assert "偏好中文回复" not in system_text

    def test_superseded_agent_memory_not_injected(self, db: Session, user_id: int):
        """agent 记忆被标 superseded → load_agent_memory 不返回（不注入）。"""
        db.add(
            UserMemory(
                user_id=user_id,
                category="fact",
                content="过时事实",
                source="agent",
                confidence=0.9,
                status="superseded",
            )
        )
        db.commit()
        from bycall.db import session_scope

        with session_scope() as s:
            assert load_agent_memory(s, user_id) == []


# ============ 触发：answered+工具调用 才提取；纯闲聊不提取 ============


def _drive_stream(
    db: Session, user: User, *, tool_calls_made: list[dict], question: str = "查一下销量"
):
    """以真实 run_agent_stream 驱动，桩掉 RAG/工具回路/压缩/提取，返回提取 mock。"""

    async def fake_tool_stream(*, chat_model, tools, messages):
        # run_tool_loop_stream 是「协程 → 返回异步生成器」结构（见 graph.py），桩需同构。
        async def _gen():
            yield None, True, GraphResult(answer="结果是 100", tool_calls_made=tool_calls_made)

        return _gen()

    extract_mock = MagicMock(return_value=0)

    async def _run():
        from bycall.agent.session_store import get_or_create_conversation

        conv = get_or_create_conversation(db, user.id, "api")
        with (
            patch("bycall.agent.runner._retrieve_across_kbs", return_value=[]),
            patch("bycall.agent.runner.run_tool_loop_stream", fake_tool_stream),
            patch("bycall.agent.runner.schedule_compaction"),
            patch("bycall.agent.runner.maybe_extract_and_save_memory", extract_mock),
        ):
            async for _chunk, _is_final, _result in run_agent_stream(
                session=db,
                user=user,
                question=question,
                llm=MagicMock(),
                channel="api",
                conversation_id=conv.id,
            ):
                pass

    asyncio.run(_run())
    return extract_mock


class TestTrigger:
    def test_answered_with_tools_triggers_extraction(self, db: Session):
        """answered 终局 + 本轮有工具调用 → 触发提取（spec 触发条件）。"""
        admin = db.exec(User.select_by_name("admin")).first()
        mock = _drive_stream(db, admin, tool_calls_made=[{"name": "kb_search", "args": {}}])
        assert mock.called
        # 传给提取的是本轮 question/answer + 正确 user_id
        _, kwargs = mock.call_args
        assert kwargs["user_id"] == admin.id
        assert "销量" in kwargs["question"]

    def test_chitchat_without_tools_does_not_trigger(self, db: Session):
        """纯闲聊（无工具调用）→ 不触发提取（成本约束：不每轮调提取 LLM）。"""
        admin = db.exec(User.select_by_name("admin")).first()
        mock = _drive_stream(db, admin, tool_calls_made=[])
        assert not mock.called


# ============ 闸门纯函数（白盒边界用例）============


class TestGateUnit:
    def _settings(self, threshold=0.85):
        return _ExtractSettings(enabled=True, confidence_threshold=threshold)

    def test_gate_pass_high_confidence_persistent(self):
        out = _gate(
            {"fact": "x", "category": "goal", "confidence": 0.86, "volatile": False},
            self._settings(),
        )
        assert out == ("x", "goal", 0.86)

    def test_gate_reject_volatile(self):
        assert (
            _gate(
                {"fact": "x", "category": "fact", "confidence": 0.99, "volatile": True},
                self._settings(),
            )
            is None
        )

    def test_gate_reject_bad_confidence_type(self):
        assert (
            _gate(
                {"fact": "x", "category": "fact", "confidence": "high", "volatile": False},
                self._settings(),
            )
            is None
        )

    def test_gate_reject_empty_fact(self):
        assert (
            _gate(
                {"fact": "  ", "category": "fact", "confidence": 0.9, "volatile": False},
                self._settings(),
            )
            is None
        )
