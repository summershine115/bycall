"""任务调度与任务 API 集成测试（S1b）。

覆盖：TaskSchedule CRUD（cron 校验 / event 必填 / 重名 409）；手动 fire 触发任务；
外部事件入口触发（goal 插值）；任务列表/详情；SSE 事件流快照回放。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlmodel import create_engine

from bycall.config import get_settings


class _FakeChatModel:
    def __init__(self, answer: str = "done"):
        self.answer = answer

    def invoke(self, messages):
        return AIMessage(content=self.answer)

    def bind_tools(self, tools):
        return self


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    get_settings.cache_clear()
    from bycall.db import init_db, set_engine

    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()

    # 打桩 chat model 解析：fire/事件入口/任务恢复都需要（避免真实 LLM 调用）。
    def fake_get_chat_model(session, **kwargs):
        return None, _FakeChatModel()

    monkeypatch.setattr("bycall.llm.resolver.get_chat_model", fake_get_chat_model)

    from bycall.admin.api import router as admin_router
    from bycall.api.events import router as events_router

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(events_router)
    return TestClient(app)


def _user(name: str = "alice") -> int:
    from bycall.db import session_scope
    from bycall.governance.models import User
    from bycall.governance.security import hash_password

    with session_scope() as s:
        u = User(name=name, display_name=name, role="member", password_hash=hash_password("x"))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id  # type: ignore[return-value]


def _seed_task(uid: int, goal: str = "种子任务", status: str = "completed") -> int:
    """直接落库一条任务（列表/详情/SSE 测试用）。"""
    from bycall.db import session_scope
    from bycall.governance.models import Step, Task

    with session_scope() as s:
        t = Task(user_id=uid, channel="api", trigger="conversation", goal=goal, status=status)
        s.add(t)
        s.commit()
        s.refresh(t)
        st = Step(task_id=t.id, step_index=0, kind="tool_loop", spec_json='{"goal": "x"}')
        s.add(st)
        s.commit()
        return t.id  # type: ignore[return-value]


class TestTaskScheduleCRUD:
    def test_create_cron_schedule(self, client):
        uid = _user()
        resp = client.post(
            "/api/admin/task-schedules",
            json={
                "name": "每日巡检",
                "trigger_type": "cron",
                "cron_expr": "0 9 * * *",
                "task_template": {"goal": "巡检系统", "plan": []},
                "user_id": uid,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "每日巡检"
        assert body["trigger_type"] == "cron"
        assert body["task_template"]["goal"] == "巡检系统"
        assert body["is_enabled"] is True

    def test_create_event_schedule_requires_source(self, client):
        uid = _user()
        resp = client.post(
            "/api/admin/task-schedules",
            json={"name": "事件调度", "trigger_type": "event", "user_id": uid},
        )
        assert resp.status_code == 400
        assert "event_source" in resp.json()["detail"]

    def test_create_invalid_cron_rejected(self, client):
        uid = _user()
        resp = client.post(
            "/api/admin/task-schedules",
            json={
                "name": "坏 cron",
                "trigger_type": "cron",
                "cron_expr": "not a cron",
                "user_id": uid,
            },
        )
        assert resp.status_code == 400

    def test_duplicate_name_conflict(self, client):
        uid = _user()
        payload = {
            "name": "同名调度",
            "trigger_type": "cron",
            "cron_expr": "0 9 * * *",
            "user_id": uid,
        }
        assert client.post("/api/admin/task-schedules", json=payload).status_code == 201
        assert client.post("/api/admin/task-schedules", json=payload).status_code == 409

    def test_unknown_user_rejected(self, client):
        resp = client.post(
            "/api/admin/task-schedules",
            json={"name": "x", "trigger_type": "cron", "cron_expr": "0 9 * * *", "user_id": 9999},
        )
        assert resp.status_code == 400

    def test_list_update_delete(self, client):
        uid = _user()
        created = client.post(
            "/api/admin/task-schedules",
            json={
                "name": "待改调度",
                "trigger_type": "cron",
                "cron_expr": "0 9 * * *",
                "user_id": uid,
            },
        ).json()
        sid = created["id"]

        rows = client.get("/api/admin/task-schedules").json()
        assert any(r["id"] == sid for r in rows)

        updated = client.patch(
            f"/api/admin/task-schedules/{sid}",
            json={"is_enabled": False, "cron_expr": "0 10 * * *"},
        ).json()
        assert updated["is_enabled"] is False
        assert updated["cron_expr"] == "0 10 * * *"

        assert client.delete(f"/api/admin/task-schedules/{sid}").status_code == 204
        assert client.get(f"/api/admin/task-schedules/{sid}").status_code == 404


class TestFireSchedule:
    def test_fire_creates_and_runs_task(self, client):
        uid = _user()
        sid = client.post(
            "/api/admin/task-schedules",
            json={
                "name": "手动触发",
                "trigger_type": "cron",
                "cron_expr": "0 9 * * *",
                "task_template": {"goal": "手动巡检"},
                "user_id": uid,
            },
        ).json()["id"]
        resp = client.post(f"/api/admin/task-schedules/{sid}/fire")
        assert resp.status_code == 200, resp.text
        task = resp.json()
        assert task["trigger"] == "cron"
        assert task["schedule_id"] == sid
        assert task["goal"] == "手动巡检"
        assert task["status"] in ("completed", "failed")

        # last_fired_at 已更新。
        sched = client.get(f"/api/admin/task-schedules/{sid}").json()
        assert sched["last_fired_at"] is not None

    def test_fire_unknown_schedule_404(self, client):
        assert client.post("/api/admin/task-schedules/9999/fire").status_code == 404


class TestEventEntry:
    def test_event_trigger_interpolates_payload(self, client):
        uid = _user()
        client.post(
            "/api/admin/task-schedules",
            json={
                "name": "订单事件",
                "trigger_type": "event",
                "event_source": "webhook-orders",
                "task_template": {"goal": "处理订单 {payload.order_id}"},
                "user_id": uid,
            },
        )
        resp = client.post("/api/events/webhook-orders", json={"payload": {"order_id": "A100"}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] in ("completed", "failed")

        from bycall.db import session_scope
        from bycall.governance.models import Task

        with session_scope() as s:
            t = s.get(Task, body["task_id"])
            assert t is not None
            assert t.goal == "处理订单 A100"
            assert t.trigger == "event"

    def test_event_unknown_source_404(self, client):
        assert client.post("/api/events/nope").status_code == 404

    def test_event_missing_payload_field_400(self, client):
        uid = _user()
        client.post(
            "/api/admin/task-schedules",
            json={
                "name": "订单事件2",
                "trigger_type": "event",
                "event_source": "webhook-orders2",
                "task_template": {"goal": "处理订单 {payload.order_id}"},
                "user_id": uid,
            },
        )
        resp = client.post("/api/events/webhook-orders2", json={"payload": {}})
        assert resp.status_code == 400

    def test_event_disabled_schedule_404(self, client):
        uid = _user()
        sid = client.post(
            "/api/admin/task-schedules",
            json={
                "name": "停用事件",
                "trigger_type": "event",
                "event_source": "webhook-off",
                "task_template": {"goal": "x"},
                "user_id": uid,
            },
        ).json()["id"]
        client.patch(f"/api/admin/task-schedules/{sid}", json={"is_enabled": False})
        assert client.post("/api/events/webhook-off").status_code == 404


class TestTaskAPI:
    def test_list_tasks(self, client):
        uid = _user()
        tid = _seed_task(uid)
        rows = client.get("/api/admin/tasks").json()
        assert any(r["id"] == tid for r in rows)
        row = next(r for r in rows if r["id"] == tid)
        assert row["user_name"] == "alice"
        assert row["trigger"] == "conversation"

    def test_list_filter_by_status(self, client):
        uid = _user()
        _seed_task(uid, status="completed")
        _seed_task(uid, goal="进行中", status="paused")
        rows = client.get("/api/admin/tasks", params={"status": "paused"}).json()
        assert len(rows) == 1
        assert rows[0]["goal"] == "进行中"

    def test_list_filter_by_conversation_before_limit(self, client):
        """关联会话先过滤再截断，避免被其他会话的新任务挤出列表。"""
        from bycall.db import session_scope
        from bycall.governance.models import Task

        uid = _user()
        related = _seed_task(uid, goal="关联任务")
        _seed_task(uid, goal="其他会话更新的任务")
        with session_scope() as session:
            task = session.get(Task, related)
            task.conversation_id = 123
            session.add(task)
        rows = client.get("/api/admin/tasks", params={"conversation_id": 123, "limit": 1}).json()
        assert [row["id"] for row in rows] == [related]
        assert client.get("/api/admin/tasks", params={"conversation_id": 999}).json() == []

    def test_task_detail_with_steps(self, client):
        uid = _user()
        tid = _seed_task(uid)
        body = client.get(f"/api/admin/tasks/{tid}").json()
        assert body["task"]["id"] == tid
        assert len(body["steps"]) == 1
        assert body["steps"][0]["kind"] == "tool_loop"

    def test_task_404(self, client):
        assert client.get("/api/admin/tasks/9999").status_code == 404


class TestTaskEventsSSE:
    """SSE 端点直接驱动测试（TestClient 流式不传播断开，无法测长连接语义）。

    直接调端点函数拿 StreamingResponse，迭代 body_iterator：
    - 首帧 = DB 回放快照（snapshot）；
    - 发布总线事件 → 下一帧为实时事件；
    - receive 返回 http.disconnect → 生成器自然结束（断开语义）。
    """

    @pytest.mark.asyncio
    async def test_sse_snapshot_then_events_then_disconnect(self, client):
        import asyncio

        from sqlmodel import Session
        from starlette.requests import Request

        from bycall.admin.api import task_events
        from bycall.agent.events import TaskEvent, default_bus
        from bycall.db import get_engine

        uid = _user()
        tid = _seed_task(uid)
        other_tid = _seed_task(uid, goal="其他任务")

        # 可控 receive：默认挂起（等事件），触发后返回 disconnect。
        state = {"disconnect": False}
        release = asyncio.Event()

        async def receive():
            if state["disconnect"]:
                return {"type": "http.disconnect"}
            await release.wait()
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/api/admin/tasks/{tid}/events",
            "headers": [],
            "query_string": b"",
            "client": ("test", 123),
            "server": ("test", 80),
        }
        request = Request(scope, receive)

        with Session(get_engine()) as session:
            resp = await task_events(task_id=tid, request=request, session=session, _admin=None)
            gen = resp.body_iterator

            # 1) 首帧：snapshot 回放（含 task + steps）。
            first = await anext(gen)
            event = json.loads(first[len("data: ") :])
            assert event["type"] == "snapshot"
            assert event["payload"]["task"]["id"] == tid
            assert len(event["payload"]["steps"]) == 1

            # 2) 发布实时事件 → 下一帧转发（总线订阅生效；其他任务事件被过滤）。
            #    订阅发生在第二次 anext（生成器进入 while 时），先启动再发布。
            next_frame = asyncio.create_task(anext(gen))
            await asyncio.sleep(0)  # 让生成器执行到 asyncio.wait（订阅已生效）
            default_bus.publish(
                TaskEvent(type="task_done", task_id=other_tid, payload={"status": "completed"})
            )
            default_bus.publish(
                TaskEvent(type="step_started", task_id=tid, step_id=1, payload={"goal": "x"})
            )
            second = await next_frame
            event = json.loads(second[len("data: ") :])
            assert event["type"] == "step_started"  # 不是 other_tid 的 task_done
            assert event["task_id"] == tid

            # 3) 客户端断开（receive 返回 http.disconnect）→ 生成器自然结束。
            state["disconnect"] = True
            release.set()
            with pytest.raises(StopAsyncIteration):
                await anext(gen)

    @pytest.mark.asyncio
    async def test_sse_disconnect_terminates(self, client):
        """客户端断开（receive 返回 http.disconnect）→ 生成器自然结束、退订总线。"""
        from sqlmodel import Session
        from starlette.requests import Request

        from bycall.admin.api import task_events
        from bycall.db import get_engine

        uid = _user()
        tid = _seed_task(uid)

        async def receive():
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/api/admin/tasks/{tid}/events",
            "headers": [],
            "query_string": b"",
            "client": ("test", 123),
            "server": ("test", 80),
        }
        request = Request(scope, receive)

        # 快照帧先到；随后生成器立即因 disconnect 退出，不需要发事件。
        with Session(get_engine()) as session:
            resp = await task_events(task_id=tid, request=request, session=session, _admin=None)
            gen = resp.body_iterator
            first = await anext(gen)
            assert json.loads(first[len("data: ") :])["type"] == "snapshot"
            with pytest.raises(StopAsyncIteration):
                await anext(gen)
