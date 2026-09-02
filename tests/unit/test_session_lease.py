"""会话租约单测——原子获取 / token 所有权 / 过期抢占 / 并发串行化 / 心跳续租。

参考 QM ``postgres-session-store`` 的语义：同一会话同一时刻只有一个 holder，过期可抢占，
释放/续租校验 token。这些测试防的是「并发双写历史」「租约泄漏（不释放）」「进程死亡后
租约永不释放」三类真实 bug。全部跑在真实 SQLite 上，验证 UPSERT 原子性在 SQLite 下成立。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, create_engine

from bycall.agent.session_lease import (
    acquire_lease,
    acquire_turn_lease,
    current_holder,
    lease_heartbeat,
    release_lease,
    release_turn_lease,
    renew_lease,
)
from bycall.db import init_db, set_engine
from bycall.governance.models import SessionLease


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    """干净的 SQLite + init_db（建表含 sessionlease）。"""
    db_path = tmp_path / "test_lease.db"
    monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
    monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
    from bycall.config import get_settings

    get_settings.cache_clear()
    set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
    init_db()
    return Session(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))


# ============ 原子获取 ============


class TestAcquire:
    def test_first_acquire_succeeds(self, db: Session):
        token = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        assert token  # 非空 token
        holder = current_holder(db, 101)
        assert holder is not None
        assert holder.token == token
        assert holder.holder == "turn"

    def test_second_acquire_while_held_returns_none(self, db: Session):
        t1 = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        t2 = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        assert t1
        assert t2 is None  # 仍被 t1 持有，不可抢
        assert current_holder(db, 101).token == t1  # 持有者不变

    def test_expired_lease_can_be_preempted(self, db: Session):
        # 直接落一条已过期的租约，模拟「上一持有者心跳停止、TTL 已过」。
        db.add(
            SessionLease(
                conversation_id=101,
                holder="turn",
                token="dead-token",
                expires_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
        db.commit()
        token = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        assert token  # 过期可抢占
        assert token != "dead-token"
        assert current_holder(db, 101).token == token


# ============ token 所有权：续租 / 释放 ============


class TestRenewRelease:
    def test_holder_can_renew(self, db: Session):
        token = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        before = current_holder(db, 101).expires_at
        assert renew_lease(db, 101, token, ttl_seconds=120) is True
        db.commit()
        after = current_holder(db, 101).expires_at
        assert after > before  # 续租延长了过期时间

    def test_wrong_token_cannot_renew(self, db: Session):
        acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        # 错误 token 续租失败（租约不属于它）
        assert renew_lease(db, 101, "not-my-token", ttl_seconds=60) is False

    def test_holder_can_release(self, db: Session):
        token = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        assert release_lease(db, 101, token) is True
        db.commit()
        assert current_holder(db, 101) is None

    def test_release_then_reacquire_available(self, db: Session):
        token = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        release_lease(db, 101, token)
        db.commit()
        t2 = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        assert t2  # 释放后立即可重新获取

    def test_wrong_token_release_is_noop(self, db: Session):
        token = acquire_lease(db, 101, "turn", ttl_seconds=60)
        db.commit()
        assert release_lease(db, 101, "intruder") is False
        db.commit()
        assert current_holder(db, 101).token == token  # 持有者未变


# ============ 并发：多线程竞争同一会话，只有一个赢家 ============


class TestConcurrency:
    def test_concurrent_acquires_exactly_one_winner(self, db: Session):
        """10 个线程同时抢同一会话租约——UPSERT 原子性保证只有一个拿到 token。

        防的 bug：读-改-写 TOCTOU 导致两个线程都以为自己拿到了租约 → 并发双写历史。
        """
        engine = db.get_bind()
        barrier = threading.Barrier(10)
        winners: list[str] = []
        lock = threading.Lock()

        def compete():
            barrier.wait()  # 尽量同时起跑
            with Session(engine) as s:
                t = acquire_lease(s, 202, "turn", ttl_seconds=60)
                s.commit()
                if t:
                    with lock:
                        winners.append(t)

        threads = [threading.Thread(target=compete) for _ in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(winners) == 1


# ============ turn 便捷封装（独立 session + 提交）============


class TestTurnWrappers:
    def test_acquire_turn_lease_blocks_second(self, db: Session):
        t1 = acquire_turn_lease(303, ttl_seconds=60)
        t2 = acquire_turn_lease(303, ttl_seconds=60)
        assert t1
        assert t2 is None
        release_turn_lease(303, t1)
        # 释放后可重新获取
        assert acquire_turn_lease(303, ttl_seconds=60)


# ============ 心跳续租 ============


class TestHeartbeat:
    def test_heartbeat_extends_expiry_while_healthy(self, db: Session):
        token = acquire_turn_lease(404, ttl_seconds=60)
        before = current_holder(db if False else Session(db.get_bind()), 404).expires_at

        async def run():
            beat = lease_heartbeat(
                404, token, ttl_seconds=60, interval_seconds=0.05, max_failures=3
            )
            task = asyncio.ensure_future(beat)
            await asyncio.sleep(0.15)  # 让心跳续租 2~3 次
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run())
        with Session(db.get_bind()) as s:
            after = current_holder(s, 404).expires_at
        assert after > before  # 心跳续租延长了过期时间

    def test_heartbeat_stops_when_lease_lost(self, db: Session):
        """租约被抢占后，旧 holder 的心跳第一次续租即失败 → 停止续租（不自欺欺人地续）。"""
        token = acquire_turn_lease(505, ttl_seconds=60)
        # 让租约过期并被别人抢走
        with Session(db.get_bind()) as s:
            lease = current_holder(s, 505)
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            s.add(lease)
            s.commit()
        acquire_turn_lease(505, ttl_seconds=60)  # 别人抢走

        async def run():
            beat = lease_heartbeat(
                505, token, ttl_seconds=60, interval_seconds=0.01, max_failures=3
            )
            # 续租失败应让心跳很快返回（不等 max_failures，因为是「被抢占」而非「DB 异常」）。
            await asyncio.wait_for(beat, timeout=1.0)

        asyncio.run(run())  # 不超时即说明心跳已因丢失而停止


# ============ 进程死亡：TTL 到期后租约可被重新获取（无需依赖 finally 释放）============


class TestDeathRecovery:
    def test_lease_expires_and_can_be_reclaimed(self, tmp_path: Path, monkeypatch):
        """模拟进程死亡：获取租约后把 expires_at 拨到过去（=TTL 耗尽），新进程可重新获取。"""
        db_path = tmp_path / "test_death.db"
        monkeypatch.setenv("BYCALL_POSTGRES_DSN", f"sqlite:///{db_path}")
        monkeypatch.setenv("BYCALL_VECTOR_STORE_DIMENSION", "16")
        from bycall.config import get_settings

        get_settings.cache_clear()
        set_engine(create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}))
        init_db()

        token = acquire_turn_lease(606, ttl_seconds=60)
        assert token
        # 模拟进程死亡后 TTL 到期：直接把租约 expires_at 拨到过去
        # （等价于心跳停止 + TTL 耗尽，避免真睡 1.2s）
        from sqlmodel import select

        from bycall.db import session_scope

        with session_scope() as s:
            lease = s.exec(select(SessionLease).where(SessionLease.conversation_id == 606)).first()
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            s.add(lease)
        # 新「进程」可抢占过期租约
        reclaimed = acquire_turn_lease(606, ttl_seconds=60)
        assert reclaimed
        assert reclaimed != token
