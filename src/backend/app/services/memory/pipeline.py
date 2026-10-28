"""记忆后台管道：对话完成后异步执行中期摘要 + 长期画像抽取（B3-8 起任务落库）。

设计：任务落库（MemoryJob），失败只记日志——记忆是增强项，绝不影响当轮对话体验。

旧实现的两个洞：
- `asyncio.create_task` 挂在进程里：正在抽取时进程重启，那几轮的摘要/画像永久缺失，
  而且没人知道丢了（页面上只看得到"没有新画像"）。
- 并发去重靠进程内 `_running` 集合：多副本部署时两个进程会对同一会话同时抽取，
  写出重复画像，还白烧一倍 token。

现在：同一会话只允许有一条 queued/running 的任务（靠带条件的 INSERT ... WHERE NOT EXISTS
原子去重）；执行前先抢行（queued→running），抢不到就说明别的进程在做；启动时把上次
留下的 running 收回成 queued 由后台补跑。
"""
from __future__ import annotations

import asyncio
import datetime

from loguru import logger
from sqlalchemy import BindParameter, text
from sqlalchemy import bindparam as sa_bindparam
from sqlalchemy.types import TypeEngine
from sqlmodel import Session

from app.db.models import MemoryJob, pk
from app.db.session import engine

# running 超过这个时长还没结束，认为执行它的进程已经没了（可被重新排队）
STALE_RUNNING_SECONDS = 600


def _utc_now() -> datetime.datetime:
    """带 tzinfo 的 UTC：SQLModel 的 UTCDateTime 列拒绝 naive datetime 绑定；
    裸 SQL 也要用列自己的类型绑（同 services/lifecycle.py 的 _DT 做法）。"""
    return datetime.datetime.now(datetime.UTC)


def _bp(name: str, value: datetime.datetime, type_: TypeEngine) -> BindParameter:
    """bindparam 薄封装：裸 SQL 的时间参数必须按列自己的类型绑定，
    否则走 sqlite3 的默认适配器（Python 3.12 起已弃用），写出的时间串与 ORM 不一致。"""
    return sa_bindparam(name, value=value, type_=type_)


_CREATED = MemoryJob.__table__.columns["created_at"].type  # type: ignore[attr-defined]
_UPDATED = MemoryJob.__table__.columns["updated_at"].type  # type: ignore[attr-defined]



def schedule_memory_jobs(conversation_id: int, user_id: int) -> int | None:
    """排队一次记忆管道；已有未完成的同会话任务时返回 None（去重）。

    去重要在 SQL 层做而不是"先查再插"：两个并发请求都查到"没有"就会各插一条。
    """
    with engine.begin() as conn:
        res = conn.execute(
            text("INSERT INTO memoryjob (conversation_id, user_id, stage, status, attempts, "
                 "created_at, updated_at) SELECT :c, :u, 'both', 'queued', 0, :n, :n "
                 "WHERE NOT EXISTS (SELECT 1 FROM memoryjob WHERE conversation_id=:c "
                 "AND status IN ('queued','running'))").bindparams(
                _bp("n", _utc_now(), _UPDATED)),
            {"c": conversation_id, "u": user_id})
        if int(res.rowcount or 0) == 0:
            return None
        job_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    _kick(job_id)
    return job_id


def _kick(job_id: int) -> None:
    """有事件循环就立刻跑（低延迟），没有就交给后台补跑循环（重启恢复的场景）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(run_job(job_id))


def claim(job_id: int) -> bool:
    """抢行执行权：queued → running。抢不到返回 False（别人在做或已做完）。"""
    with engine.begin() as conn:
        res = conn.execute(
            text("UPDATE memoryjob SET status='running', attempts=attempts+1, updated_at=:n "
                 "WHERE id=:id AND status='queued'").bindparams(_bp("n", _utc_now(), _UPDATED)),
            {"id": job_id})
        return int(res.rowcount or 0) == 1


def finish(job_id: int, status: str, error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE memoryjob SET status=:s, error=:e, updated_at=:n WHERE id=:id"
                 ).bindparams(_bp("n", _utc_now(), _UPDATED)),
            {"s": status, "e": error[:300] if error else None, "id": job_id})


def _work(conversation_id: int, user_id: int) -> None:
    from app.config import get_settings
    from app.services.memory import long as long_mem
    from app.services.memory import mid

    with Session(engine) as session:
        mid.ensure_summary(session, conversation_id)
        if bool(get_settings().get_dotted("memory.long_enabled", True)):
            long_mem.extract_facts(session, conversation_id, user_id)


async def run_job(job_id: int) -> None:
    """执行一条任务（LLM 调用是同步阻塞的，放线程池避免拖慢事件循环）。"""
    if not claim(job_id):
        return
    with Session(engine) as session:
        job = session.get(MemoryJob, job_id)
        if job is None:
            return
        conv_id, user_id = job.conversation_id, job.user_id
    try:
        await asyncio.to_thread(_work, conv_id, user_id)
        finish(job_id, "done")
    except Exception as e:
        logger.debug("记忆管道失败（忽略）: {}", e)
        finish(job_id, "failed", str(e))


def run_pending_blocking(limit: int = 3) -> int:
    """同步排干若干条 queued 任务（供后台补跑线程与冒烟脚本调用）。"""
    ran = 0
    with Session(engine) as session:
        from sqlmodel import col, select

        rows = session.exec(select(MemoryJob).where(MemoryJob.status == "queued")
                            .order_by(col(MemoryJob.id)).limit(limit)).all()
        jobs = [(pk(r), r.conversation_id, r.user_id) for r in rows]
    for job_id, conv_id, user_id in jobs:
        if not claim(job_id):
            continue
        try:
            _work(conv_id, user_id)
            finish(job_id, "done")
            ran += 1
        except Exception as e:
            logger.debug("记忆补跑失败（忽略）: {}", e)
            finish(job_id, "failed", str(e))
    return ran


def recover_memory_jobs() -> int:
    """启动自检：把上次进程留下的 running 收回成 queued，交给补跑循环。"""
    with engine.begin() as conn:
        res = conn.execute(
            text("UPDATE memoryjob SET status='queued', updated_at=:n, "
                 "error='进程重启导致任务中断，已重新排队' WHERE status='running'"
                 ).bindparams(_bp("n", _utc_now(), _UPDATED)))
        n = int(res.rowcount or 0)
    if n:
        logger.warning("启动自检: {} 个记忆任务被收回重跑", n)
    return n


def reclaim_stale_running(seconds: int = STALE_RUNNING_SECONDS) -> int:
    """把长时间卡在 running 的任务收回（持它的进程被 kill 又没走启动自检的情形）。"""
    cutoff = _utc_now() - datetime.timedelta(seconds=seconds)
    with engine.begin() as conn:
        res = conn.execute(
            text("UPDATE memoryjob SET status='queued', updated_at=:n "
                 "WHERE status='running' AND updated_at < :c").bindparams(
                _bp("n", _utc_now(), _UPDATED), _bp("c", cutoff, _UPDATED)))
    return int(res.rowcount or 0)


async def memory_backfill_loop() -> None:
    """后台补跑循环：租约选主，每 30s 清一批 queued（重启丢失的即时触发靠这里兜底）。"""
    from app.services import lease

    while True:
        try:
            if lease.acquire("memory_pipeline", ttl_seconds=90):
                reclaim_stale_running()
                await asyncio.to_thread(run_pending_blocking, 3)
        except Exception:
            logger.exception("记忆补跑循环失败")
        await asyncio.sleep(30)


def status_counts(session: Session) -> dict[str, int]:
    from sqlmodel import func, select

    rows = session.exec(select(MemoryJob.status, func.count())
                        .group_by(MemoryJob.status)).all()
    return {str(r[0]): int(r[1] or 0) for r in rows}
