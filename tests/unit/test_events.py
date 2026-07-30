"""typed event + 事件总线测试（S1a T2）。"""

from __future__ import annotations

from bycall.agent.events import TaskEvent, TaskEventBus


def _evt(type_: str = "task_created", task_id: int = 1, **payload) -> TaskEvent:
    """构造一条事件（payload 透传）。"""
    return TaskEvent(type=type_, task_id=task_id, payload=dict(payload))


# ============ TaskEvent 模型 ============


class TestTaskEvent:
    def test_defaults(self):
        """新建事件取全部默认值。"""
        e = TaskEvent(type="task_created", task_id=1)
        assert e.type == "task_created"
        assert e.task_id == 1
        assert e.step_id is None
        assert e.run_id is None
        assert e.payload == {}
        assert e.trace_id is None
        assert e.occurred_at is not None

    def test_payload_and_trace(self):
        """payload 带事件特定数据；trace_id 预留 OTel。"""
        e = _evt("tool_called", tool_name="search", args={"q": "x"})
        assert e.payload["tool_name"] == "search"
        assert e.payload["args"] == {"q": "x"}
        e2 = TaskEvent(type="step_started", task_id=1, trace_id="abc-123")
        assert e2.trace_id == "abc-123"

    def test_all_event_types_constructible(self):
        """spec §3.3 全部事件类型都能构造（Literal 类型校验）。"""
        for t in [
            "task_created",
            "plan_produced",
            "step_started",
            "tool_called",
            "approval_requested",
            "step_completed",
            "step_failed",
            "task_done",
            "task_paused",
        ]:
            assert TaskEvent(type=t, task_id=1).type == t


# ============ 事件总线 ============


class TestEventBus:
    def test_subscribe_by_type(self):
        """按类型订阅，只收匹配的事件。"""
        bus = TaskEventBus()
        got = []
        bus.subscribe("step_completed", got.append)
        bus.publish(_evt("step_completed", result="ok"))
        bus.publish(_evt("step_started"))
        assert len(got) == 1
        assert got[0].payload["result"] == "ok"

    def test_wildcard_subscribes_all(self):
        """None 通配订阅收全部。"""
        bus = TaskEventBus()
        all_events = []
        typed = []
        bus.subscribe(None, all_events.append)
        bus.subscribe("step_failed", typed.append)
        bus.publish(_evt("task_created"))
        bus.publish(_evt("step_failed", err="boom"))
        bus.publish(_evt("task_done"))
        assert len(all_events) == 3
        assert len(typed) == 1

    def test_handler_exception_isolated(self):
        """一个 handler 抛错，其他 handler 仍收到，publish 不传播异常。"""
        bus = TaskEventBus()
        ok = []

        def boom(_):
            raise RuntimeError("boom")

        bus.subscribe("step_completed", boom)
        bus.subscribe("step_completed", ok.append)
        bus.publish(_evt("step_completed"))  # 不应抛
        assert len(ok) == 1

    def test_unsubscribe(self):
        """退订后不再收到。"""
        bus = TaskEventBus()
        got = []
        h = got.append
        bus.subscribe("task_done", h)
        bus.unsubscribe("task_done", h)
        bus.publish(_evt("task_done"))
        assert got == []

    def test_publish_no_subscribers_ok(self):
        """无订阅者 publish 不报错。"""
        bus = TaskEventBus()
        bus.publish(_evt("task_created"))

    def test_multiple_handlers_same_type(self):
        """同类型多个 handler 都收到。"""
        bus = TaskEventBus()
        a, b = [], []
        bus.subscribe("step_started", a.append)
        bus.subscribe("step_started", b.append)
        bus.publish(_evt("step_started"))
        assert len(a) == 1 and len(b) == 1

    def test_typed_and_wildcard_both_fire(self):
        """同一事件既触发类型订阅者又触发通配订阅者。"""
        bus = TaskEventBus()
        typed, wild = [], []
        bus.subscribe("tool_called", typed.append)
        bus.subscribe(None, wild.append)
        bus.publish(_evt("tool_called", tool_name="x"))
        assert len(typed) == 1 and len(wild) == 1
