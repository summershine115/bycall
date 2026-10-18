"""性能回归验证：查询规模、事件循环活性、事务与有界后台任务，不断言机器耗时。"""

from __future__ import annotations

import asyncio
import gc
import json
import subprocess
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from bycall.agent import runner
from bycall.agent.context_budget import ContextBudget
from bycall.agent.graph import GraphResult
from bycall.agent.memory_writer import maybe_extract_and_save_memory
from bycall.agent.session_store import load_agent_memory, load_user_memory
from bycall.governance.capability import CapabilitySet
from bycall.governance.models import AuditLog, Document, SystemSetting, User, UserMemory
from bycall.mcp.context import get_current_capability
from bycall.rag.publication import visible_generations
from bycall.tools.approval import get_approval_context
from bycall.vector_store.models import Chunk, SearchRequest
from bycall.vector_store.zvec_store import ZvecStore


@pytest.fixture
def engine(tmp_path):
    # 单连接池会暴露「入口未归还连接，后台再申请」的生命周期错误。
    engine = create_engine(
        f"sqlite:///{tmp_path / 'perf.db'}",
        connect_args={"check_same_thread": False},
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize("count", [10, 1000])
def test_publication_constant_query_count_and_acl(engine, count):
    with Session(engine) as session:
        session.add_all(
            Document(knowledge_base_id=1, title=str(i), source="public", status="completed")
            for i in range(count)
        )
        session.add_all(
            [
                Document(knowledge_base_id=1, title="secret", source="secret", status="completed"),
                Document(
                    knowledge_base_id=1, title="level", source="public", level=5, status="completed"
                ),
                Document(knowledge_base_id=1, title="draft", source="public", status="processing"),
                Document(knowledge_base_id=2, title="other", source="public", status="completed"),
                SystemSetting(key="_rag.index.1", value=json.dumps({"active": "generation-1"})),
                SystemSetting(key="_rag.index.2", value=json.dumps({"active": None})),
            ]
        )
        session.commit()
    queries = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, *args: queries.append(sql))
    with Session(engine) as session:
        allowed = visible_generations(session, 1, ["public"], 0, False)
        assert len(queries) == 1
        assert "document.content" not in queries[0]
        assert set(allowed) == {"generation-1"} | {f"doc_{i}" for i in range(3, count + 1)}
        assert visible_generations(session, 1, [], 0, False) == []
        assert len(visible_generations(session, 1, ["*"], 0, False)) == count
        assert len(visible_generations(session, 1, [], 0, True)) == count + 1


def test_memory_ranking_and_budget_preserved(engine):
    with Session(engine) as session:
        session.add_all(
            [
                UserMemory(user_id=1, category="fact", content="无关"),
                UserMemory(user_id=1, category="fact", content="销售" * 20),
                UserMemory(user_id=1, category="fact", content="销售职责"),
                UserMemory(user_id=1, category="fact", content="销售", status="superseded"),
                UserMemory(user_id=2, category="fact", content="销售"),
                UserMemory(user_id=1, category="fact", content="长" * 100, source="agent"),
                UserMemory(user_id=1, category="fact", content="简短", source="agent"),
            ]
        )
        session.commit()
        budget = ContextBudget(memory_max_items=2, memory_max_chars=6, agent_memory_max_chars=5)
        assert [m.content for m in load_user_memory(session, 1, "销售", budget=budget)] == [
            "销售职责",
            "无关",
        ]
        assert [m.content for m in load_agent_memory(session, 1, budget=budget)] == ["简短"]
        assert load_user_memory(session, 1, budget=ContextBudget(memory_max_items=0)) == []
        assert load_agent_memory(session, 1, budget=ContextBudget(agent_memory_max_items=0)) == []


def test_extraction_releases_connection_and_deduplicates_batch(engine, monkeypatch):
    import bycall.db as db

    monkeypatch.setattr(db, "_engine", engine)
    facts = [
        {"fact": fact, "category": "fact", "confidence": 0.9, "volatile": False}
        for fact in ("偏好 中文", "偏好中文", "负责销售")
    ]

    class Model:
        def invoke(self, messages):
            assert engine.pool.checkedout() == 0
            return SimpleNamespace(content=json.dumps(facts))

    queries = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, *args: queries.append(sql))
    assert maybe_extract_and_save_memory(llm=Model(), user_id=1, question="x", answer="y") == 2
    assert len([sql for sql in queries if sql.startswith("SELECT usermemory.content")]) == 1


def test_stream_preparation_keeps_loop_live_and_context_local(engine, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    cap = CapabilitySet(
        user_id=1, user_name="alice", role="member", knowledge_scope_tags=["public"]
    )
    with Session(engine) as session:
        user = User(id=1, name="alice", role="member")
        session.add(user)
        session.commit()

    def retrieve(session, *args):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(3)
        return []

    async def tool_loop(**kwargs):
        assert get_current_capability() == cap
        assert get_approval_context().user_id == 1
        # 前置读取已结束，生成答案期间没有 DB 连接被挂住。
        assert engine.pool.checkedout() == 0

        async def frames():
            yield "answer", False, None
            yield "", True, GraphResult(answer="answer")

        return frames()

    monkeypatch.setattr(runner, "compute_capability_set", lambda *a: cap)
    monkeypatch.setattr(runner, "_retrieve_across_kbs", retrieve)
    monkeypatch.setattr(runner, "run_tool_loop_stream", tool_loop)

    async def check():
        with Session(engine) as session:
            user = session.get(User, 1)

            async def consume():
                return [
                    frame
                    async for frame in runner.run_agent_stream(
                        session=session, user=user, question="question", llm=MagicMock()
                    )
                ]

            task = asyncio.create_task(consume())
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert not task.done()
                assert get_current_capability() is None
                release.set()
                frames = await asyncio.wait_for(task, 3)
                assert frames[-1][2].decision == "answered"
                assert get_current_capability() is None
                assert get_approval_context() is None
            finally:
                release.set()
                await task

    asyncio.run(check())


@pytest.mark.parametrize("streamed", [False, True])
def test_answer_finishes_before_optional_extraction(engine, monkeypatch, streamed):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runner, "_MEMORY_SYNC", False)
    monkeypatch.setattr(runner, "_retrieve_across_kbs", lambda *a: [])
    cap = CapabilitySet(
        user_id=1, user_name="alice", role="member", knowledge_scope_tags=["public"]
    )
    monkeypatch.setattr(runner, "compute_capability_set", lambda *a: cap)
    result = GraphResult(answer="answer", tool_calls_made=[{"name": "clock", "args": {}}])
    monkeypatch.setattr(runner, "run_tool_loop", lambda **kw: result)

    async def loop(**kw):
        async def frames():
            yield "", True, result

        return frames()

    monkeypatch.setattr(runner, "run_tool_loop_stream", loop)

    def extract(**kwargs):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(runner, "maybe_extract_and_save_memory", extract)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runner, "_background_executor", executor)
        with Session(engine) as session:
            user = User(id=1, name="alice", role="member")
            session.add(user)
            session.commit()
            args = {"session": session, "user": user, "question": "question", "llm": MagicMock()}
            try:
                if streamed:

                    async def consume():
                        return [f async for f in runner.run_agent_stream(**args)]

                    assert asyncio.run(consume())[-1][2].decision == "answered"
                else:
                    assert runner.run_agent(**args).decision == "answered"
                assert entered.wait(1)
                with Session(engine) as reader:
                    assert reader.exec(select(AuditLog)).first().decision == "answered"
            finally:
                release.set()


def test_maintenance_bounded_and_compaction_coalesced(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runner, "_COMPACTION_SYNC", False)
    monkeypatch.setattr(runner, "_background_slots", threading.BoundedSemaphore(2))
    monkeypatch.setattr(runner, "_compactor_pending", set())

    def blocked(*args):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(runner, "_run_compaction", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runner, "_background_executor", executor)
        try:
            runner.schedule_compaction(1)
            assert entered.wait(1)
            runner.schedule_compaction(1)
            assert runner._compactor_pending == {1}
            future = runner._submit_maintenance(blocked)
            assert future is not None
            assert runner._submit_maintenance(blocked) is None
            runner.schedule_compaction(2)
            assert runner._compactor_pending == {1}
        finally:
            release.set()
    assert runner._compactor_pending == set()
    assert runner._background_slots.acquire(blocking=False)
    assert runner._background_slots.acquire(blocking=False)
    runner._background_slots.release()
    runner._background_slots.release()


def test_zvec_close_releases_write_lock_without_gc(tmp_path, monkeypatch):
    store = ZvecStore(str(tmp_path), 8)
    store.open("probe")
    store.insert([Chunk(id="one", text="probe", vector=[1.0] + [0.0] * 7)])
    store.search(SearchRequest(vector=[1.0] + [0.0] * 7))
    collection = weakref.ref(store._collection)
    monkeypatch.setattr(gc, "collect", lambda *args: pytest.fail("不应全堆 GC"))
    store.close()
    assert collection() is None
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,zvec; c=zvec.open(sys.argv[1]); assert c.stats.doc_count == 1",
            str(tmp_path / "probe"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    # 纯检索后的关闭不调用 flush。
    store.open("probe")
    store.search(SearchRequest(vector=[1.0] + [0.0] * 7))
    monkeypatch.setattr(store._collection, "flush", lambda: pytest.fail("纯读不刷盘"))
    store.close()


def test_cancelled_preparation_keeps_slot_until_worker_exits(engine, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(runner, "_prepare_slots", slots)
    cap = CapabilitySet(
        user_id=1, user_name="alice", role="member", knowledge_scope_tags=["public"]
    )
    monkeypatch.setattr(runner, "compute_capability_set", lambda *a: cap)

    def retrieve(*args):
        entered.set()
        assert release.wait(3)
        return []

    monkeypatch.setattr(runner, "_retrieve_across_kbs", retrieve)
    with Session(engine) as session:
        session.add(User(id=1, name="alice", role="member"))
        session.commit()
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(runner, "_prepare_executor", executor)

        async def check():
            with Session(engine) as session:
                user = session.get(User, 1)

                async def consume():
                    return [
                        f
                        async for f in runner.run_agent_stream(
                            session=session, user=user, question="x", llm=MagicMock()
                        )
                    ]

                task = asyncio.create_task(consume())
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert not slots.acquire(blocking=False)
                    assert get_current_capability() is None
                finally:
                    release.set()

        asyncio.run(check())
    # executor 退出已等待线程和完成回调；取消不造成泄漏或提前超售。
    assert slots.acquire(blocking=False)
    slots.release()
    with Session(engine) as session:
        assert session.exec(select(AuditLog)).first() is None


def test_invisible_kb_does_not_trigger_query_rewrite(engine, monkeypatch):
    from bycall.agent.session_store import get_or_create_conversation, save_message
    from bycall.governance.models import KnowledgeBase

    cap = CapabilitySet(
        user_id=1,
        user_name="alice",
        role="member",
        knowledge_scope_tags=["public"],
        visible_kb_ids=[],
    )
    monkeypatch.setattr(runner, "compute_capability_set", lambda *a: cap)
    monkeypatch.setattr(runner, "run_tool_loop", lambda **kw: GraphResult(answer="answer"))
    with Session(engine) as session:
        user = User(id=1, name="alice", role="member")
        session.add(user)
        session.add(
            KnowledgeBase(
                name="private", collection_name="private", dimension=8, embedding_model_id=1
            )
        )
        session.commit()
        conv = get_or_create_conversation(session, 1, "api")
        save_message(session, conv.id, "user", "销售流程")
        session.commit()
        model = MagicMock()
        assert (
            runner.run_agent(
                session=session,
                user=user,
                question="它需要审批吗",
                llm=model,
                conversation_id=conv.id,
            ).decision
            == "answered"
        )
        model.invoke.assert_not_called()


@pytest.fixture
def conversation_id(engine):
    from bycall.agent.session_store import get_or_create_conversation, save_message

    with Session(engine) as session:
        conversation = get_or_create_conversation(session, 1, "api")
        id_ = conversation.id
        for i in range(24):
            save_message(session, id_, "user" if i % 2 == 0 else "assistant", f"内容{i}")
        session.commit()
        return id_


@pytest.mark.parametrize("reply", ["new summary", "", RuntimeError("model unavailable")])
def test_compaction_waits_without_connection_and_only_advances_on_success(
    engine, conversation_id, reply
):
    from bycall.agent.session_store import compress_conversation
    from bycall.governance.models import Conversation, SessionLease

    class Model:
        def invoke(self, messages):
            assert engine.pool.checkedout() == 0
            # 租约已提交，对独立读取可见。
            with Session(engine) as reader:
                lease = reader.exec(select(SessionLease)).one()
                assert lease.holder == "compaction"
            if isinstance(reply, Exception):
                raise reply
            return SimpleNamespace(content=reply)

    with Session(engine) as session:
        assert compress_conversation(session, conversation_id, Model()) is bool(
            reply == "new summary"
        )
        conv = session.get(Conversation, conversation_id)
        assert conv.summary_covered_count == (4 if reply == "new summary" else 0)
        assert conv.summary == ("new summary" if reply == "new summary" else "")
        assert session.exec(select(SessionLease)).first() is None


def test_compaction_respects_turn_lease(engine, conversation_id):
    from bycall.agent.session_lease import acquire_lease
    from bycall.agent.session_store import compress_conversation

    with Session(engine) as session:
        assert acquire_lease(session, conversation_id, "turn", ttl_seconds=60)
        session.commit()
        model = MagicMock()
        assert compress_conversation(session, conversation_id, model) is False
        model.invoke.assert_not_called()


@pytest.mark.parametrize("conflict", ["lease", "expired", "summary"])
def test_stale_compaction_does_not_overwrite_or_release_new_owner(
    engine, conversation_id, conflict
):
    from sqlalchemy import update

    from bycall.agent.session_store import compress_conversation
    from bycall.governance.models import Conversation, SessionLease

    class Model:
        def invoke(self, messages):
            with Session(engine) as writer:
                if conflict == "lease":
                    writer.execute(update(SessionLease).values(token="new-owner"))
                elif conflict == "expired":
                    from datetime import UTC, datetime, timedelta

                    writer.execute(
                        update(SessionLease).values(
                            expires_at=datetime.now(UTC) - timedelta(seconds=1)
                        )
                    )
                else:
                    writer.execute(update(Conversation).values(summary="newer summary"))
                writer.commit()
            return SimpleNamespace(content="stale summary")

    with Session(engine) as session:
        assert compress_conversation(session, conversation_id, Model()) is False
        conv = session.get(Conversation, conversation_id)
        assert conv.summary_covered_count == 0
        assert conv.summary == ("newer summary" if conflict == "summary" else "")
        if conflict == "lease":
            assert session.exec(select(SessionLease)).one().token == "new-owner"
