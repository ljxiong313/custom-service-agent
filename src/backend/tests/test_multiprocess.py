"""B3-8 多进程协调单测：租约选主与记忆任务队列。

这两件事的失败模式都是"平时看不出来、上多副本才炸"，所以断言集中在：
同一时刻只有一个持约者、过期可被接管、任务不会被两个进程重复执行、重启能收回中断。
"""
from __future__ import annotations

import datetime

import pytest
from sqlmodel import Session, select

from app.db.models import Conversation, MemoryJob, User, utcnow
from app.db.session import engine
from app.services import lease
from app.services.memory import pipeline


@pytest.fixture
def target(db: Session) -> tuple[int, int]:
    """(conversation_id, user_id)：memoryjob 有外键，必须用真实行。"""
    user = User(username="mp_owner", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="多进程用例")
    db.add(conv)
    db.commit()
    return (int(conv.id), int(user.id))


# ---- 租约 ----

def test_lease_is_exclusive_and_refixable():
    other = "someone-else:1:abcd"
    assert lease.acquire("unit_test_lease", ttl_seconds=60) is True
    # 同一 holder 续约仍然 True（持约者每轮都要重新抢）
    assert lease.acquire("unit_test_lease", ttl_seconds=60) is True
    assert lease.acquire("unit_test_lease", ttl_seconds=60, holder=other) is False
    info = lease.current_holder("unit_test_lease")
    assert info["mine"] is True and info["epoch"] == 2


def test_expired_lease_can_be_taken_over():
    other = "another:2:ffff"
    assert lease.acquire("takeover", ttl_seconds=60) is True
    # ttl 为负：立刻过期，模拟持约进程被 kill 后不再续约
    assert lease.acquire("takeover", ttl_seconds=-1) is True
    assert lease.acquire("takeover", ttl_seconds=60, holder=other) is True
    assert lease.acquire("takeover", ttl_seconds=60) is False
    assert lease.current_holder("takeover")["holder"] == other


def test_release_only_clears_own_lease():
    other = "third:3:eeee"
    lease.acquire("rel", ttl_seconds=60)
    lease.release("rel", holder=other)                  # 不是我的约，不该动
    assert lease.current_holder("rel")["holder"] == lease.HOLDER_ID
    lease.release("rel")                                # 我的约：清空让别人能拿
    assert lease.acquire("rel", ttl_seconds=60, holder=other) is True


def test_lease_unknown_name_reports_empty():
    assert lease.current_holder("never_taken") == {"name": "never_taken", "holder": "",
                                                   "epoch": 0, "expires_at": None,
                                                   "mine": False}


# ---- 记忆任务队列 ----

def test_schedule_dedupes_per_conversation(target):
    conv_id, user_id = target
    first = pipeline.schedule_memory_jobs(conv_id, user_id)
    assert first is not None
    # 同会话还有一条 queued/running → 不再排第二条（多进程并发也不会双插，靠 WHERE NOT EXISTS）
    assert pipeline.schedule_memory_jobs(conv_id, user_id) is None
    with Session(engine) as session:
        assert len(session.exec(select(MemoryJob)
                                .where(MemoryJob.conversation_id == conv_id)).all()) == 1


def test_claim_is_single_owner(target):
    conv_id, user_id = target
    job_id = pipeline.schedule_memory_jobs(conv_id, user_id)
    assert job_id is not None
    assert pipeline.claim(job_id) is True
    assert pipeline.claim(job_id) is False          # 已经被别的循环抢走了
    pipeline.finish(job_id, "done")
    assert pipeline.claim(job_id) is False          # 终态不可再抢
    with Session(engine) as session:
        row = session.get(MemoryJob, job_id)
        assert row is not None and row.status == "done" and row.attempts == 1


def test_run_pending_blocking_finishes_jobs(target, monkeypatch):
    conv_id, user_id = target
    calls: list[int] = []
    monkeypatch.setattr(pipeline, "_work", lambda c, u: calls.append(c))
    job_id = pipeline.schedule_memory_jobs(conv_id, user_id)
    assert job_id is not None
    assert pipeline.run_pending_blocking(limit=5) == 1
    assert calls == [conv_id]
    # 已经跑完的任务不会被再次排队/执行，避免"补跑循环"退化成反复烧 token
    assert pipeline.run_pending_blocking(limit=5) == 0
    assert calls == [conv_id]


def test_work_failure_marks_failed_and_keeps_reason(target, monkeypatch):
    conv_id, user_id = target

    def boom(c, u):
        raise RuntimeError("embedding 401")

    monkeypatch.setattr(pipeline, "_work", boom)
    job_id = pipeline.schedule_memory_jobs(conv_id, user_id)
    assert pipeline.run_pending_blocking(limit=1) == 0
    with Session(engine) as session:
        row = session.get(MemoryJob, int(job_id))
        assert row is not None and row.status == "failed" and "embedding 401" in (row.error or "")


def test_recover_and_reclaim_stale(target):
    conv_id, user_id = target
    job_id = pipeline.schedule_memory_jobs(conv_id, user_id)
    pipeline.claim(int(job_id))                     # 假装正在跑
    assert pipeline.recover_memory_jobs() == 1      # 进程重启：running → queued
    assert pipeline.reclaim_stale_running(seconds=0) == 0   # 已回到 queued，没有 stale

    # 直接造一条"三天前开始 running"的行，验证长时间卡住也会被收回
    with Session(engine) as session:
        stale = MemoryJob(conversation_id=conv_id, user_id=user_id, status="running",
                          updated_at=utcnow() - datetime.timedelta(days=3),
                          created_at=utcnow() - datetime.timedelta(days=3))
        session.add(stale)
        session.commit()
        stale_id = int(stale.id)
    assert pipeline.reclaim_stale_running(seconds=600) == 1
    with Session(engine) as session:
        row = session.get(MemoryJob, stale_id)
        assert row is not None and row.status == "queued"


def test_status_counts_shape(target, monkeypatch):
    conv_id, user_id = target
    monkeypatch.setattr(pipeline, "_work", lambda c, u: None)
    job_id = pipeline.schedule_memory_jobs(conv_id, user_id)
    pipeline.run_pending_blocking(limit=1)
    with Session(engine) as session:
        counts = pipeline.status_counts(session)
    assert counts.get("done") == 1 and counts.get("queued", 0) == 0
    assert job_id is not None
