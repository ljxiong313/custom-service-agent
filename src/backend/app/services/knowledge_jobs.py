"""知识库重建任务：状态落库 + 有界并发执行。

为什么不用 BackgroundTasks（旧实现）：它把协程/线程挂在当前进程上，进程重启后任务消失，
但 KnowledgeFile.status 还停在 parsing，页面既查不到进度、也重新点不动（B1-4 的卡死）。

现在任务本身是 IngestJob 表里的一行：
- queued 只有被 worker 取出才转 running，所以"在跑"= 本进程真的有一个线程在跑它；
- 进程重启时 running/queued 一律判为 interrupted（recover_interrupted_jobs），可原地重试；
- 并发数由配置 rag.ingest_workers 限制（默认 2），避免整库重建时 20 个任务同时打
  embedding API 触发限流。
"""
from __future__ import annotations

import queue
import threading

from loguru import logger
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import IngestJob, KnowledgeFile, pk, utcnow
from app.db.session import engine

# 单进程内的任务队列：本模块只在 uvicorn 单 worker 下使用（见 README「部署形态」）
_pending: queue.Queue[int] = queue.Queue()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_stop = threading.Event()

KIND_UPLOAD = "upload"
KIND_REINGEST = "reingest"
KIND_REINDEX = "reindex"


def _max_workers() -> int:
    """并发上限。yaml 里写空值/写错类型都不能让 worker 启动炸掉（get_dotted 只在键缺失时给默认值）。"""
    raw = get_settings().get_dotted("rag.ingest_workers", 2)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("配置 rag.ingest_workers={!r} 不是数字，按 1 处理", raw)
        return 1


def create_job(session: Session, file: KnowledgeFile, kind: str, total: int = 0) -> IngestJob:
    job = IngestJob(file_id=pk(file), file_name=file.name, kind=kind,
                    status="queued", total=total)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def update_progress(session: Session, job: IngestJob, processed: int, total: int | None = None) -> None:
    """给页面轮询用的进度写回（每批 chunk 一次，不必每个 chunk 一次）。"""
    job.processed = processed
    if total is not None:
        job.total = total
    job.updated_at = utcnow()   # 页面上"最后一次心跳"，否则一个跑了两分钟的任务看起来像卡住
    session.add(job)
    session.commit()


def finish_job(session: Session, job: IngestJob, status: str = "done", error: str | None = None) -> None:
    job.status = status
    job.error = (error or "")[:300] or None
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def active_job_ids(session: Session) -> set[int]:
    """仍在排队/执行的任务对应的 file_id：接口据此拒绝同一文件的重复重建。"""
    rows = session.exec(select(IngestJob).where(col(IngestJob.status).in_(("queued", "running")))).all()
    return {r.file_id for r in rows}


def _set_file_status(session: Session, file_id: int, status: str, error: str | None = None) -> None:
    kf = session.get(KnowledgeFile, file_id)
    if not kf:
        return
    kf.status = status
    if status == "failed":
        kf.error = (error or "")[:300] or kf.error
    elif status == "ready":
        kf.error = None
    session.add(kf)
    session.commit()


def _run_job(job_id: int) -> None:
    """在线程里执行一个任务：自己开 Session（线程间不能共用请求会话）。"""
    from app.services.rag import ingest

    with Session(engine) as session:
        job = session.get(IngestJob, job_id)
        if not job or job.status not in ("queued", "interrupted"):
            return                      # 已被别的轮次取走或已终结
        kf = session.get(KnowledgeFile, job.file_id)
        if not kf:
            finish_job(session, job, "failed", "文件已删除")
            return
        job.status = "running"
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
        _set_file_status(session, kf.id or 0, "parsing")
        logger.info("重建任务开始: job#{} file#{} kind={}", job_id, kf.id, job.kind)
        try:
            if job.kind == KIND_REINDEX:
                n = ingest.reindex_file(session, kf,
                                        progress=lambda p, t: update_progress(session, job, p, t))
                finish_job(session, job, "done")
                _set_file_status(session, kf.id or 0, "ready")
                logger.info("重建任务完成(向量): job#{} file#{} 向量={}", job_id, kf.id, n)
            else:
                # reingest 才在任务内 purge：接口里先 purge 会让排队中的文件立刻失去语料
                ingest.ingest_file(session, kf,
                                   purge_first=(job.kind == KIND_REINGEST),
                                   progress=lambda p, t: update_progress(session, job, p, t))
                done = "failed" if kf.status == "failed" else "done"
                finish_job(session, job, done, kf.error)
                logger.info("重建任务结束: job#{} file#{} status={} chunks={}",
                            job_id, kf.id, done, kf.chunk_count)
        except Exception as e:  # noqa: BLE001 - 任务失败要落库，不能让 worker 线程带着状态消失
            logger.exception("重建任务异常: job#{} file#{}", job_id, kf.id if kf else "?")
            finish_job(session, job, "failed", str(e))
            _set_file_status(session, job.file_id, "failed", str(e))


def _worker_loop() -> None:
    while not _stop.is_set():
        try:
            job_id = _pending.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            _run_job(job_id)
        except Exception:  # noqa: BLE001 - worker 线程不能因单个任务崩溃而退出
            logger.exception("重建 worker 异常")
        finally:
            _pending.task_done()


def enqueue(job_id: int) -> None:
    """提交任务并在需要时懒启动 worker 线程池。"""
    global _worker_thread
    _ensure_workers()
    _pending.put(job_id)


def _ensure_workers() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _stop.clear()
        # 用单条 dispatcher 线程 + 有界线程池反而更复杂；这里直接起 N 条 worker 线程，
        # 每条各自从队列取任务，进程内并发即等于 rag.ingest_workers。
        n = max(1, _max_workers())
        t = threading.Thread(target=_pool_main, args=(n,), name="ingest-workers", daemon=True)
        _worker_thread = t
        t.start()


def _pool_main(n: int) -> None:
    threads = [threading.Thread(target=_worker_loop, name=f"ingest-worker-{i}", daemon=True)
               for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()


def stop_workers() -> None:
    """关停时调用：worker 是 daemon 线程，进程退出即散，这里只置停止位让循环先退出。"""
    _stop.set()


def worker_alive() -> bool:
    """重建 worker 线程是否在跑（懒启动：从没排过队时确实是 False，只作信息不作就绪条件）。"""
    with _worker_lock:
        return bool(_worker_thread and _worker_thread.is_alive())


def recover_interrupted_jobs() -> int:
    """进程启动自检：把上次进程留下的 running/queued 判为 interrupted。

    这些任务对应的 KnowledgeFile.status 也停在 parsing，必须一起收回，
    否则页面永远显示"入库中"且 _reject_busy 会拒绝任何重试。
    """
    with Session(engine) as session:
        rows = session.exec(select(IngestJob).where(col(IngestJob.status).in_(("queued", "running")))).all()
        for job in rows:
            job.status = "interrupted"
            job.error = "进程重启导致任务中断，可重新发起"
            session.add(job)
            kf = session.get(KnowledgeFile, job.file_id)
            if kf and kf.status == "parsing":
                kf.status = "failed"
                kf.error = "上次入库/重建被进程重启打断，请重新发起"
                session.add(kf)
        session.commit()
        if rows:
            logger.warning("启动自检: {} 个知识库任务被标记为 interrupted（可重试）", len(rows))
        return len(rows)


def recent_jobs(session: Session, limit: int = 20) -> list[dict]:
    rows = session.exec(select(IngestJob).order_by(col(IngestJob.id).desc()).limit(limit)).all()
    return [{"id": j.id, "file_id": j.file_id, "file_name": j.file_name, "kind": j.kind,
             "status": j.status, "processed": j.processed, "total": j.total,
             "error": j.error, "created_at": j.created_at.isoformat(),
             "updated_at": j.updated_at.isoformat()} for j in rows]


def pending_count(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(IngestJob)
                            .where(col(IngestJob.status).in_(("queued", "running")))).one())


def status_counts(session: Session) -> dict[str, int]:
    """各状态任务数（设置页"系统状态"用：一眼看出有没有卡在中断/失败）。"""
    rows = session.exec(select(IngestJob.status, func.count())
                        .group_by(IngestJob.status)).all()
    return {str(status): int(total) for status, total in rows}
